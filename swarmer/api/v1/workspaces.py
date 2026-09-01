"""REST API — Workspace CRUD."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s, workspace_acl
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.api.deps import (
    filter_accessible_workspaces,
    get_workspace_or_404,
    require_api_auth,
)
from swarmer.api.schemas import (
    MessageOut,
    ParseGatewayCommandIn,
    ParseGatewayCommandOut,
    ParseTokenIn,
    ParseTokenOut,
    TestGatewayConnectionIn,
    TestGatewayConnectionOut,
    WorkspaceCreate,
    WorkspaceGatewayCreate,
    WorkspaceGatewayOut,
    WorkspaceMemberCreate,
    WorkspaceMemberOut,
    WorkspaceOut,
    WorkspaceUpdate,
)
from swarmer.k8s_auth import TokenIdentity
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_gateway import WorkspaceGateway
from swarmer.models.workspace_member import WorkspaceMember
from swarmer.openshell_command_parser import parse_gateway_command_or_json
from swarmer.openshell_oidc import oidc_manager
from swarmer.openshell_token_parser import parse_token_input

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces",
    tags=["workspaces"],
    dependencies=[Depends(require_api_auth)],
)


def _derive_namespace(display_name: str) -> str:
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:63]


def _serialize_gateway(gw: WorkspaceGateway) -> WorkspaceGatewayOut:
    """Build a WorkspaceGatewayOut, exposing only has_* booleans for secrets."""
    return WorkspaceGatewayOut(
        workspace_id=gw.workspace_id,
        gateway_url=gw.gateway_url,
        auth_mode=gw.auth_mode,
        oidc_issuer=gw.oidc_issuer,
        oidc_client_id=gw.oidc_client_id,
        oidc_audience=gw.oidc_audience,
        has_refresh_token=bool(gw.refresh_token_enc),
        has_access_token=bool(gw.access_token_enc),
        access_token_expires_at=gw.access_token_expires_at,
        has_bearer_token=bool(gw.bearer_token_enc),
        has_tls_cert=bool(gw.tls_cert),
        has_tls_key=bool(gw.tls_key_enc),
        tls_ca=gw.tls_ca,
        tls_verify=gw.tls_verify,
        created_at=gw.created_at,
        updated_at=gw.updated_at,
    )


def _to_gateway_out(gw: WorkspaceGateway | None) -> WorkspaceGatewayOut | None:
    if gw is None or not gw.gateway_url:
        return None
    return _serialize_gateway(gw)


def _to_workspace_out(ws: Workspace, gw: WorkspaceGateway | None = None) -> WorkspaceOut:
    gw_out = None
    if gw is not None:
        gw_out = _to_gateway_out(gw)
    elif "gateway" in ws.__dict__ and ws.__dict__["gateway"] is not None:
        gw_out = _to_gateway_out(ws.__dict__["gateway"])

    return WorkspaceOut(
        id=ws.id,
        display_name=ws.display_name,
        namespace=ws.namespace,
        description=ws.description,
        owner_id=ws.owner_id,
        gateway=gw_out,
        created_at=ws.created_at,
        updated_at=ws.updated_at,
    )


# ============================================================
# Gateway Parsing & Testing Helpers
# ============================================================


@router.post("/gateway/parse-command", response_model=ParseGatewayCommandOut)
async def parse_gateway_command_endpoint(body: ParseGatewayCommandIn):
    res = parse_gateway_command_or_json(body.command)
    return ParseGatewayCommandOut(
        gateway_url=res.gateway_url,
        auth_mode=res.auth_mode,
        oidc_issuer=res.oidc_issuer,
        oidc_client_id=res.oidc_client_id,
        oidc_audience=res.oidc_audience,
        bearer_token=res.bearer_token,
        tls_verify=res.tls_verify,
        suggested_name=res.suggested_name,
        errors=res.errors,
    )


@router.post("/gateway/parse-token", response_model=ParseTokenOut)
async def parse_gateway_token_endpoint(body: ParseTokenIn):
    res = parse_token_input(body.token_input)
    return ParseTokenOut(
        refresh_token=res.refresh_token,
        access_token=res.access_token,
        expires_at=res.expires_at,
        issuer=res.issuer,
        client_id=res.client_id,
        format_detected=res.format_detected,
        status=res.status,
        message=res.message,
        char_count=res.char_count,
    )


@router.post("/gateway/test-connection", response_model=TestGatewayConnectionOut)
async def test_gateway_connection_endpoint(
    body: TestGatewayConnectionIn,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
):
    from swarmer.openshell_client import GatewayConfig, probe_gateway_connectivity
    from swarmer.openshell_oidc import OidcGatewayAuth

    stored_gw: WorkspaceGateway | None = None
    if body.workspace_id is not None:
        ws = (
            (
                await db.execute(
                    select(Workspace)
                    .where(Workspace.id == body.workspace_id)
                    .options(selectinload(Workspace.gateway))
                )
            )
            .scalars()
            .first()
        )
        if ws is None or not await workspace_acl.user_can_access_workspace(
            db, ws, identity.username, identity.groups
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {body.workspace_id} not found",
            )
        if not await workspace_acl.can_manage_members(
            db, ws, identity.username, identity.groups
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the workspace owner or an admin can test stored gateway credentials.",
            )
        stored_gw = ws.gateway

    oidc_issuer = body.oidc_issuer or (stored_gw.oidc_issuer if stored_gw else None)
    oidc_client_id = body.oidc_client_id or (stored_gw.oidc_client_id if stored_gw else None)
    oidc_audience = body.oidc_audience or (stored_gw.oidc_audience if stored_gw else None)
    refresh_token = body.refresh_token
    if not refresh_token and stored_gw is not None and stored_gw.auth_mode == "oidc":
        refresh_token = stored_gw.refresh_token or None
    bearer_token = body.bearer_token
    if not bearer_token and stored_gw is not None and stored_gw.auth_mode == "bearer":
        bearer_token = stored_gw.bearer_token or None

    uses_stored_credential = (
        (body.auth_mode == "oidc" and body.refresh_token is None and bool(refresh_token))
        or (body.auth_mode == "bearer" and body.bearer_token is None and bool(bearer_token))
    )
    if uses_stored_credential and stored_gw is not None:
        requested_url = body.gateway_url.strip()
        stored_url = (stored_gw.gateway_url or "").strip()
        if requested_url != stored_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "When reusing stored gateway credentials, gateway_url must match "
                    "the workspace's saved gateway URL."
                ),
            )

    temp_auth = None
    bearer_callable = None
    if body.auth_mode == "oidc" and oidc_issuer and oidc_client_id and refresh_token:
        temp_auth = OidcGatewayAuth(
            issuer=oidc_issuer,
            client_id=oidc_client_id,
            audience=oidc_audience or "",
            tls_ca=body.tls_ca,
        )
        temp_auth.seed(refresh_token)
        bearer_callable = temp_auth.current_access_token

    config = GatewayConfig(
        gateway_url=body.gateway_url.strip(),
        auth_mode=body.auth_mode,
        tls_ca=body.tls_ca,
        tls_cert=body.tls_cert,
        tls_key=body.tls_key,
        tls_verify=body.tls_verify,
        bearer_token=bearer_token if body.auth_mode == "bearer" else None,
        bearer_callable=bearer_callable,
    )
    try:
        result = await probe_gateway_connectivity(config)
        return TestGatewayConnectionOut(
            status="ok",
            gateway_url=config.gateway_url,
            auth_mode=config.auth_mode,
            sandboxes_count=result.get("sandboxes_count", 0),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connection test failed: {exc}",
        )
    finally:
        if temp_auth is not None:
            temp_auth.close()


# ============================================================
# Workspace CRUD
# ============================================================


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
):
    result = await db.execute(
        select(Workspace).options(selectinload(Workspace.gateway)).order_by(Workspace.display_name)
    )
    workspaces = result.scalars().all()
    accessible = await filter_accessible_workspaces(db, workspaces, identity)
    return [_to_workspace_out(ws) for ws in accessible]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
):
    if settings.k8s_namespace:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace creation is disabled in namespace-scoped deployments.",
        )
    if not await workspace_acl.can_create_workspace(db, identity.username, identity.groups):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create workspaces.",
        )
    namespace = _derive_namespace(body.display_name)
    if not namespace:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Display name must contain at least one alphanumeric character.",
        )

    ws = Workspace(
        display_name=body.display_name.strip(),
        namespace=namespace,
        description=body.description.strip(),
        owner_id=identity.username,
    )
    db.add(ws)

    gw: WorkspaceGateway | None = None
    if body.gateway and body.gateway.gateway_url:
        gw = WorkspaceGateway(
            gateway_url=body.gateway.gateway_url.strip(),
            auth_mode=body.gateway.auth_mode or "oidc",
            oidc_issuer=body.gateway.oidc_issuer,
            oidc_client_id=body.gateway.oidc_client_id,
            oidc_audience=body.gateway.oidc_audience,
            tls_ca=body.gateway.tls_ca,
            tls_cert=body.gateway.tls_cert,
            tls_verify=body.gateway.tls_verify,
        )
        if body.gateway.refresh_token:
            gw.refresh_token = body.gateway.refresh_token
        if body.gateway.access_token:
            gw.access_token = body.gateway.access_token
        if body.gateway.bearer_token:
            gw.bearer_token = body.gateway.bearer_token
        if body.gateway.tls_key:
            gw.tls_key = body.gateway.tls_key
        ws.gateway = gw

    try:
        await db.commit()
        await db.refresh(ws)
        if gw is not None:
            await db.refresh(gw)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace with namespace '{namespace}' already exists.",
        )

    return _to_workspace_out(ws, gw=gw)


@router.get("/{ws_id}", response_model=WorkspaceOut)
async def get_workspace(
    ws_id: int,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
):
    result = await db.execute(
        select(Workspace).where(Workspace.id == ws_id).options(selectinload(Workspace.gateway))
    )
    ws = result.scalar_one_or_none()
    if ws is None or not await workspace_acl.user_can_access_workspace(db, ws, identity.username, identity.groups):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {ws_id} not found",
        )
    return _to_workspace_out(ws)


async def _require_manage_permission(
    db: AsyncSession, ws: Workspace, identity: TokenIdentity
) -> None:
    """Raise 403 unless *identity* is the workspace owner or a configured admin."""
    if not await workspace_acl.can_manage_members(
        db, ws, identity.username, identity.groups
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner or an admin can do that.",
        )


@router.put("/{ws_id}", response_model=WorkspaceOut)
async def update_workspace(
    body: WorkspaceUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    ws.display_name = body.display_name.strip()
    ws.description = body.description.strip()
    await db.commit()
    await db.refresh(ws)
    return _to_workspace_out(ws)


@router.delete("/{ws_id}", response_model=MessageOut)
async def delete_workspace(
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    name = ws.display_name
    k8s_ns = ws.k8s_namespace

    # Invalidate cached in-memory OIDC gateway auth manager
    oidc_manager.invalidate(ws.id)

    # Delete DB row first to avoid orphaned rows if K8s cleanup fails
    await db.delete(ws)
    await db.commit()

    try:
        if not settings.k8s_namespace:
            k8s.delete_namespace(k8s_ns)
    except Exception:
        log.warning("Failed to delete K8s namespace %s for workspace '%s'", k8s_ns, name)

    return MessageOut(detail=f"Workspace '{name}' deleted.")


# ============================================================
# Dedicated Workspace Gateway Endpoints
# ============================================================


@router.get("/{ws_id}/gateway", response_model=WorkspaceGatewayOut)
async def get_workspace_gateway(
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == ws.id)
    )
    gw = result.scalar_one_or_none()
    if gw is None or not gw.gateway_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This workspace uses the default cluster OpenShell gateway.",
        )
    return _serialize_gateway(gw)


@router.post("/{ws_id}/gateway", response_model=WorkspaceGatewayOut)
async def set_workspace_gateway(
    body: WorkspaceGatewayCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    result = await db.execute(
        select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == ws.id)
    )
    gw = result.scalar_one_or_none()
    if gw is None:
        gw = WorkspaceGateway(workspace_id=ws.id)
        db.add(gw)

    gw.gateway_url = body.gateway_url.strip()
    gw.auth_mode = body.auth_mode or "oidc"
    gw.oidc_issuer = body.oidc_issuer
    gw.oidc_client_id = body.oidc_client_id
    gw.oidc_audience = body.oidc_audience
    gw.tls_ca = body.tls_ca
    gw.tls_cert = body.tls_cert
    gw.tls_verify = body.tls_verify

    if body.refresh_token is not None:
        gw.refresh_token = body.refresh_token
    if body.access_token is not None:
        gw.access_token = body.access_token
    if body.bearer_token is not None:
        gw.bearer_token = body.bearer_token
    if body.tls_key is not None:
        gw.tls_key = body.tls_key

    oidc_manager.invalidate(ws.id)
    await db.commit()
    await db.refresh(gw)
    return _serialize_gateway(gw)


@router.delete("/{ws_id}/gateway", response_model=MessageOut)
async def delete_workspace_gateway(
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    result = await db.execute(
        select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == ws.id)
    )
    gw = result.scalar_one_or_none()
    if gw is not None:
        await db.delete(gw)
        oidc_manager.invalidate(ws.id)
        await db.commit()
    return MessageOut(detail="Workspace reverted to default cluster OpenShell gateway.")


# ============================================================
# Members (ACM-41659) — database-backed workspace ACL
# ============================================================


@router.get("/{ws_id}/members", response_model=list[WorkspaceMemberOut])
async def list_workspace_members(
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws.id)
        .order_by(WorkspaceMember.user_id)
    )
    return result.scalars().all()


@router.post(
    "/{ws_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_member(
    body: WorkspaceMemberCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    user_id = body.user_id.strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id is required.",
        )
    if user_id == ws.owner_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{user_id}' is already the workspace owner.",
        )
    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=user_id,
        role=(body.role or "member").strip() or "member",
    )
    db.add(member)
    try:
        await db.commit()
        await db.refresh(member)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{user_id}' is already a member of this workspace.",
        )
    return member


@router.delete("/{ws_id}/members/{user_id}", response_model=MessageOut)
async def remove_workspace_member(
    user_id: str,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{user_id}' is not a member of this workspace.",
        )
    await db.delete(member)
    await db.commit()
    return MessageOut(detail=f"'{user_id}' removed from workspace.")
