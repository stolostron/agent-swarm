"""REST API — MCP server management."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.api.deps import (
    get_current_user,
    get_workspace_or_404,
    require_api_auth,
    require_human_api_auth,
)
from swarmer.api.schemas import (
    McpHealthOut,
    McpServerAddFromCatalog,
    McpServerOut,
    McpServerSaveConfig,
    MessageOut,
)
from swarmer.database import get_db
from swarmer.k8s_auth import TokenIdentity
from swarmer.mcp_catalog import get_catalog_entry
from swarmer.models.mcp_server import McpServer
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{ws_id}/mcp-servers",
    tags=["mcp-servers"],
    dependencies=[Depends(require_api_auth)],
)


def _visible_servers_filter(ws_id: int, user: str):
    """Return the WHERE clause for MCP servers visible to the given user."""
    return (
        McpServer.workspace_id == ws_id,
        or_(
            McpServer.user_id == user,
            McpServer.shared == True,  # noqa: E712
            McpServer.user_id == "",
        ),
    )


async def _get_visible_server_or_404(
    ws_id: int, server_id: int, user: str, db: AsyncSession,
) -> McpServer:
    """Load a single MCP server by ID, enforcing user visibility."""
    result = await db.execute(
        select(McpServer).where(
            McpServer.id == server_id,
            *_visible_servers_filter(ws_id, user),
        )
    )
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return server


async def _require_workspace_manager(
    db: AsyncSession, ws: Workspace, identity: TokenIdentity,
) -> None:
    """Require a human workspace manager (owner, workspace admin, or global
    admin) for shared mutations."""
    from swarmer import workspace_acl

    if not await workspace_acl.can_manage_members(
        db, ws, identity.username, identity.groups
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace managers can update shared MCP servers.",
        )


@router.get("", response_model=list[McpServerOut])
async def list_mcp_servers(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(McpServer).where(
            *_visible_servers_filter(ws_id, user),
        ).order_by(McpServer.display_name)
    )
    return result.scalars().all()


@router.post("", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def add_from_catalog(
    ws_id: int,
    body: McpServerAddFromCatalog,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_human_api_auth),
) -> McpServer:
    """Create a catalog server shared with workspace members."""
    await _require_workspace_manager(db, ws, identity)
    entry = get_catalog_entry(body.catalog_slug)
    if entry is None:
        raise HTTPException(status_code=400, detail="Unknown MCP server type")

    server = McpServer(
        workspace_id=ws_id,
        slug=entry["slug"],
        display_name=entry["display_name"],
        server_url=entry.get("server_url", ""),
        server_type=entry.get("server_type", "http"),
        jira_server_url=entry.get("default_jira_server_url", ""),
        user_id=identity.username,
        shared=True,
    )
    db.add(server)
    try:
        await db.commit()
        await db.refresh(server)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"'{entry['display_name']}' is already added to this workspace.",
        )

    return server


@router.post("/{server_id}/save", response_model=McpServerOut)
async def save_config(
    ws_id: int,
    server_id: int,
    body: McpServerSaveConfig,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_human_api_auth),
):
    await _require_workspace_manager(db, ws, identity)
    server = await _get_visible_server_or_404(ws_id, server_id, identity.username, db)

    jira_server_url = body.jira_server_url.strip().rstrip("/")
    jira_access_token = body.jira_access_token.strip()
    jira_email = body.jira_email.strip()

    if not jira_server_url or not jira_email:
        raise HTTPException(status_code=422, detail="Server URL and email are required")

    # Resolve the workspace gateway before changing ORM fields: the lookup
    # queries WorkspaceGateway on this session and would otherwise autoflush
    # these changes before the provider RPC completes. Workspaces on the
    # default gateway get None here; resolve the shared default client so the
    # reconciliation lock and provider calls key on a real endpoint.
    try:
        from swarmer import openshell_client

        gateway_client = await openshell_client.get_client_for_workspace(ws_id, db)
        if gateway_client is None:
            gateway_client = openshell_client._get_client()
    except Exception as exc:
        log.warning(
            "save_config: failed to configure Jira provider for workspace %d (error_type=%s)",
            ws_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="failed to configure Jira provider on OpenShell",
        ) from exc

    provider_name = f"swarmer-ws-{ws_id}-jira"
    # End the read transaction used for authorization/gateway lookup before
    # waiting on another save, then read the authoritative saved state under
    # the same lock used by provider reconciliation and deletion.
    await db.rollback()
    async with openshell_client.provider_reconciliation_lock(
        provider_name, gateway_client
    ):
        ws = await get_workspace_or_404(ws_id, db, identity)
        await _require_workspace_manager(db, ws, identity)
        server = await _get_visible_server_or_404(
            ws_id, server_id, identity.username, db
        )
        if not jira_access_token and not server.jira_access_token_enc:
            raise HTTPException(status_code=422, detail="API token is required")

        previous_jira_server_url = server.jira_server_url
        previous_jira_email = server.jira_email
        previous_jira_access_token = server.jira_access_token
        saved_token = jira_access_token or previous_jira_access_token

        from swarmer.routers.mcp_servers import _probe_jira_token

        valid = await _probe_jira_token(jira_server_url, jira_email, saved_token)
        server.jira_server_url = jira_server_url
        server.shared = True
        if jira_access_token:
            server.jira_access_token = jira_access_token
        server.jira_email = jira_email
        if valid:
            server.token_expires_at = None
        else:
            from datetime import datetime, timezone

            server.token_expires_at = datetime.now(timezone.utc)

        async def restore_previous_state() -> None:
            try:
                await db.rollback()
            except BaseException as exc:  # noqa: BLE001 - rollback must include cancellation
                log.warning(
                    "save_config: database rollback failed for workspace %d "
                    "(error_type=%s)",
                    ws_id,
                    type(exc).__name__,
                )
            try:
                if previous_jira_access_token:
                    await openshell_client.ensure_provider(
                        provider_name,
                        "jira",
                        config={
                            "JIRA_SERVER_URL": previous_jira_server_url,
                            "JIRA_EMAIL": previous_jira_email,
                        },
                        credentials={
                            "JIRA_ACCESS_TOKEN": previous_jira_access_token
                        },
                        client=gateway_client,
                        force_reconcile=True,
                        lock_held=True,
                    )
                else:
                    await openshell_client.delete_provider(
                        provider_name, client=gateway_client, lock_held=True
                    )
            except BaseException as exc:  # noqa: BLE001 - log compensation failures without masking original error
                log.error(
                    "save_config: failed to restore Jira provider after save failure "
                    "for workspace %d (error_type=%s)",
                    ws_id,
                    type(exc).__name__,
                )

        provider_synced = False
        commit_succeeded = False
        try:
            await openshell_client.ensure_provider(
                provider_name,
                "jira",
                config={
                    "JIRA_SERVER_URL": jira_server_url,
                    "JIRA_EMAIL": jira_email,
                },
                credentials={"JIRA_ACCESS_TOKEN": saved_token},
                client=gateway_client,
                lock_held=True,
            )
            provider_synced = True
            # A cancelled wait does not mean a failed commit: aiosqlite runs
            # queued commits in its worker even after the awaiting future is
            # cancelled. Keep the commit task alive and decide restoration
            # from its real outcome while still holding the provider lock.
            commit_task = asyncio.create_task(db.commit())
            try:
                await asyncio.shield(commit_task)
                commit_succeeded = True
            except asyncio.CancelledError:
                while not commit_task.done():
                    try:
                        await asyncio.shield(commit_task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:  # noqa: BLE001 - outcome probe only
                        break
                try:
                    commit_task.result()
                    commit_succeeded = True
                except BaseException:  # noqa: BLE001 - the commit failed
                    pass
                raise
        except BaseException as exc:  # noqa: BLE001 - compensation must include cancellation
            # Keep the provider write, DB commit, and compensation in one
            # critical section. Cancellation is included because a worker
            # thread may finish its RPC after the request task is cancelled.
            # A commit that actually landed must not be rolled back on the
            # gateway — only restore when persistence really failed.
            if not commit_succeeded:
                await restore_previous_state()
            if not provider_synced and isinstance(exc, Exception):
                log.warning(
                    "save_config: failed to configure Jira provider for workspace %d "
                    "(error_type=%s)",
                    ws_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="failed to configure Jira provider on OpenShell",
                ) from exc
            raise

    await db.refresh(server)
    return server


@router.get("/check", response_model=McpHealthOut)
async def check_health(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(McpServer).where(*_visible_servers_filter(ws_id, user))
    )
    servers = result.scalars().all()

    statuses = {}
    for srv in servers:
        if srv.jira_access_token_enc:
            from swarmer.routers.mcp_servers import _probe_jira_token
            valid = await _probe_jira_token(
                srv.jira_server_url, srv.jira_email, srv.jira_access_token
            )
            if valid and srv.token_expires_at is not None:
                srv.token_expires_at = None
            elif not valid and srv.token_expires_at is None:
                from datetime import datetime, timezone
                srv.token_expires_at = datetime.now(timezone.utc)

        statuses[str(srv.id)] = {
            "status": srv.auth_status,
            "label": srv.auth_status_label,
            "color": srv.auth_status_color,
        }

    await db.commit()
    return McpHealthOut(statuses=statuses)


@router.post("/{server_id}/toggle", response_model=McpServerOut)
async def toggle_server(
    ws_id: int,
    server_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_human_api_auth),
):
    await _require_workspace_manager(db, ws, identity)
    server = await _get_visible_server_or_404(ws_id, server_id, identity.username, db)

    server.enabled = not server.enabled
    server.shared = True
    await db.commit()
    await db.refresh(server)
    return server


@router.delete("/{server_id}", response_model=MessageOut)
async def delete_mcp_server(
    ws_id: int,
    server_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_human_api_auth),
):
    await _require_workspace_manager(db, ws, identity)
    server = await _get_visible_server_or_404(ws_id, server_id, identity.username, db)

    name = server.display_name
    await db.delete(server)
    await db.commit()
    return MessageOut(detail=f"Removed {name}.")
