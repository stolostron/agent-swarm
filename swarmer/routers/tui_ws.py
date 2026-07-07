"""WebSocket endpoint that proxies a browser xterm.js terminal to an OpenShell sandbox using the ExecSandboxInteractive gRPC stream."""
import asyncio
import json
import logging
import shlex
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from swarmer.database import get_db
from swarmer.models.session import Session

router = APIRouter()
log = logging.getLogger(__name__)


@router.websocket("/ws/{ws_id}/sessions/{sid}/tui")
async def session_tui(
    websocket: WebSocket,
    ws_id: int,
    sid: int,
    cols: int = 80,
    rows: int = 24,
):
    await websocket.accept()

    # ---------- Authenticate via one-time token ----------
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="Auth timeout")
        return

    session_data = websocket.session
    tui_tokens: list = session_data.get("tui_tokens", [])
    if token not in tui_tokens:
        log.warning("TUI WS: invalid/missing token for session %d (have %d tokens)", sid, len(tui_tokens))
        await websocket.close(code=4001, reason="Invalid token")
        return
    tui_tokens.remove(token)
    session_data["tui_tokens"] = tui_tokens

    # ---------- Load session from DB ----------
    async for db in get_db():
        session = await db.get(Session, sid)
        break

    if session is None or session.workspace_id != ws_id:
        await websocket.close(code=4002, reason="Session not found")
        return

    if session.phase != "running" or not session.sandbox_name:
        log.warning(
            "TUI WS: session %d not running (phase=%s, sandbox=%s)",
            sid, session.phase, session.sandbox_name,
        )
        await websocket.close(code=4003, reason="Session not running")
        return

    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(session.agent_tool)

    tui_cmd_parts = [tool.get_tui_binary()]
    _tui_model = session.model or ""
    if _tui_model and tool.name != "crush":
        tui_cmd_parts.extend(["--model", _tui_model])
    cmd_base = " ".join(shlex.quote(p) for p in tui_cmd_parts)
    tui_shell = (
        f"export HOME=/sandbox PATH=\"/sandbox/.local/bin:$PATH\" && "
        f"{{ {cmd_base} --continue || exec {cmd_base}; }}"
    )

    loop = asyncio.get_running_loop()
    read_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    stop_event = threading.Event()

    await _run_openshell_tui(
        websocket=websocket,
        session=session,
        tui_shell=tui_shell,
        cols=cols,
        rows=rows,
        loop=loop,
        read_q=read_q,
        stop_event=stop_event,
    )


async def _run_openshell_tui(
    websocket: WebSocket,
    session: Session,
    tui_shell: str,
    cols: int,
    rows: int,
    loop,
    read_q: asyncio.Queue,
    stop_event: threading.Event,
) -> None:
    from swarmer import openshell_client
    from openshell._proto import openshell_pb2

    sandbox_name = session.sandbox_name

    # Resolve sandbox_id synchronously (brief blocking call)
    try:
        sandbox_id = await openshell_client._sandbox_id(sandbox_name, openshell_client._get_client())
    except Exception as exc:
        log.error("TUI: sandbox_id lookup failed for %s: %s", sandbox_name, exc)
        await websocket.close(code=4004, reason="Sandbox lookup failed")
        return

    # Inject workspace extra env vars (arbitrary key-value pairs stored in DB) and
    # Jira MCP non-secret config (JIRA_SERVER_URL, JIRA_EMAIL).
    # NOTE: provider credentials (GOOGLE_API_KEY, JIRA_ACCESS_TOKEN, GH_TOKEN etc.)
    # are attached as providers at sandbox creation and injected by the gateway supervisor
    # as opaque reference tokens — they ARE inherited by exec calls.
    # However, JIRA_SERVER_URL and JIRA_EMAIL are plain env vars (not provider credentials)
    # and must be passed explicitly on every ExecSandboxRequest.
    tui_env: dict[str, str] = {}
    try:
        from sqlalchemy import select as _sa_select
        from swarmer.database import get_db as _get_db
        from swarmer.models.sandbox_env_var import SandboxEnvVar
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers as _get_mcp
        async for db in _get_db():
            _ev_result = await db.execute(
                _sa_select(SandboxEnvVar).where(
                    SandboxEnvVar.workspace_id == session.workspace_id
                )
            )
            for ev_row in _ev_result.scalars().all():
                tui_env[ev_row.key] = ev_row.value
            for mcp in await _get_mcp(session.workspace_id, db):
                if "jira" in getattr(mcp, "slug", "") and getattr(mcp, "jira_access_token_enc", ""):
                    if mcp.jira_server_url:
                        tui_env["JIRA_SERVER_URL"] = mcp.jira_server_url
                    if mcp.jira_email:
                        tui_env["JIRA_EMAIL"] = mcp.jira_email
                    break
            break
    except Exception:
        log.warning("TUI: failed to load workspace env vars for session %d", session.id, exc_info=True)
    # Point OpenCode at the config file written at sandbox setup time.
    # OPENCODE_CONFIG is the env-var equivalent of --config (there is no CLI flag).
    if session.agent_tool == "opencode":
        tui_env["OPENCODE_CONFIG"] = "/sandbox/opencode.json"

    command = ["sh", "-c", tui_shell]

    try:
        client = openshell_client._get_client()
        response_stream, input_q = openshell_client.exec_interactive(
            sandbox_name=sandbox_name,
            sandbox_id=sandbox_id,
            command=command,
            cols=cols,
            rows=rows,
            env=tui_env or None,
            client=client,
        )
    except Exception as exc:
        log.error("TUI: exec_interactive failed for sandbox %s: %s", sandbox_name, exc)
        await websocket.close(code=4004, reason="Exec failed")
        return

    def _stream_reader() -> None:
        """Drain the gRPC response stream into read_q (background thread)."""
        try:
            for event in response_stream:
                if stop_event.is_set():
                    break
                which = event.WhichOneof("payload")
                if which == "stdout":
                    data = event.stdout.data
                    if data:
                        loop.call_soon_threadsafe(read_q.put_nowait, data)
                elif which == "stderr":
                    data = event.stderr.data
                    if data:
                        chunk = b"\r\n\x1b[31m" + data + b"\x1b[0m"
                        loop.call_soon_threadsafe(read_q.put_nowait, chunk)
                elif which == "exit":
                    break
        except Exception as exc:
            if not stop_event.is_set():
                log.error("TUI gRPC stream reader error for sandbox %s: %s", sandbox_name, exc)
        finally:
            loop.call_soon_threadsafe(read_q.put_nowait, None)

    reader_thread = threading.Thread(target=_stream_reader, daemon=True)
    reader_thread.start()

    async def read_loop() -> None:
        try:
            while True:
                chunk = await read_q.get()
                if chunk is None:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def write_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("bytes"):
                    input_q.put(openshell_pb2.ExecSandboxInput(stdin=msg["bytes"]))
                elif msg.get("text"):
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            input_q.put(openshell_pb2.ExecSandboxInput(
                                resize=openshell_pb2.ExecSandboxWindowResize(
                                    cols=payload.get("cols", 80),
                                    rows=payload.get("rows", 24),
                                )
                            ))
                    except Exception:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    read_task = asyncio.create_task(read_loop())
    write_task = asyncio.create_task(write_loop())

    try:
        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        log.error("TUI proxy error for sandbox %s: %s", sandbox_name, exc)
    finally:
        stop_event.set()
        input_q.put(None)  # stop the gRPC request generator
        reader_thread.join(timeout=2.0)
        try:
            await websocket.close()
        except Exception:
            pass



