"""
WebSocket endpoint that proxies a browser xterm.js terminal to a session pod
using the Kubernetes Python client exec stream (no kubectl subprocess needed).
"""
import asyncio
import json
import logging
import shlex
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

router = APIRouter()
log = logging.getLogger(__name__)

_STDIN_CHANNEL = 0
_RESIZE_CHANNEL = 4


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
        session = await db.get(
            Session,
            sid,
            options=[selectinload(Session.workspace)],
        )
        ws = await db.get(Workspace, ws_id)
        break

    if session is None or ws is None or session.workspace_id != ws_id:
        await websocket.close(code=4002, reason="Session not found")
        return

    if not session.pod_name or session.phase != "running":
        log.warning("TUI WS: session %d not running (phase=%s, pod=%s)", sid, session.phase if session else "none", session.pod_name if session else "none")
        await websocket.close(code=4003, reason="Session not running")
        return

    namespace = ws.k8s_namespace
    pod_name = session.pod_name

    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(session.agent_tool)
    container_name = tool.get_container_name()

    tui_cmd_parts = [tool.get_tui_binary()]
    if session.model and hasattr(tool, 'get_tui_model_args'):
        tui_cmd_parts.extend(tool.get_tui_model_args(session.model))
    elif session.model and tool.name != "crush":
        tui_cmd_parts.extend(["--model", session.model])
    cmd_base = " ".join(shlex.quote(p) for p in tui_cmd_parts)
    tui_shell = (
        f"export PATH=\"$HOME/.local/bin:$PATH\" && "
        f"{{ {cmd_base} --continue || exec {cmd_base}; }}"
    )

    # ---------- Open kubernetes exec stream ----------
    from kubernetes import client as k8s_client
    from kubernetes.stream import stream as k8s_stream

    v1 = k8s_client.CoreV1Api()
    try:
        exec_resp = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container_name,
            command=["sh", "-c", tui_shell],
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
            _preload_content=False,
        )
    except Exception as exc:
        log.error("TUI exec stream open failed for pod %s: %s", pod_name, exc)
        try:
            await websocket.close(code=4004, reason="Exec failed")
        except Exception:
            pass
        return

    # Send initial terminal size (channel 4 = resize)
    try:
        exec_resp.write_channel(
            _RESIZE_CHANNEL, json.dumps({"Width": cols, "Height": rows})
        )
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    read_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    stop_event = threading.Event()

    def _stream_reader() -> None:
        """Pump pod stdout/stderr into read_q (runs in a background thread)."""
        try:
            while not stop_event.is_set() and exec_resp.is_open():
                exec_resp.update(timeout=0.1)
                if exec_resp.peek_stdout():
                    data = exec_resp.read_stdout()
                    if data:
                        chunk = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
                        loop.call_soon_threadsafe(read_q.put_nowait, chunk)
                if exec_resp.peek_stderr():
                    data = exec_resp.read_stderr()
                    if data:
                        chunk = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
                        loop.call_soon_threadsafe(read_q.put_nowait, b"\r\n\x1b[31m" + chunk + b"\x1b[0m")
        except Exception as exc:
            if not stop_event.is_set():
                log.error("TUI stream reader error for pod %s: %s", pod_name, exc)
        finally:
            if not stop_event.is_set():
                log.info("TUI stream reader: exec closed for pod %s", pod_name)
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
                    # Raw bytes from xterm.js → pod stdin (channel 0)
                    exec_resp.write_channel(_STDIN_CHANNEL, msg["bytes"])
                elif msg.get("text"):
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            exec_resp.write_channel(
                                _RESIZE_CHANNEL,
                                json.dumps({
                                    "Width": payload.get("cols", 80),
                                    "Height": payload.get("rows", 24),
                                }),
                            )
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
        log.error("TUI proxy error for pod %s: %s", pod_name, exc)
    finally:
        stop_event.set()
        try:
            exec_resp.close()
        except Exception:
            pass
        reader_thread.join(timeout=2.0)
        try:
            await websocket.close()
        except Exception:
            pass
