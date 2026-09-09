"""OpenShell gateway version observation and provider-state invalidation."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.openshell_gateway_version import OpenShellGatewayVersion
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_gateway import WorkspaceGateway

log = logging.getLogger(__name__)


async def observe_gateway_version(config, client, db: AsyncSession) -> str:
    """Record a gateway version and invalidate stale AI provider references.

    OpenShell owns the AI credentials, so a gateway replacement can remove the
    providers without changing Swarmer's database. A version transition is the
    explicit signal that the configured-state flags must be cleared.
    """
    from openshell._proto import openshell_pb2

    try:
        info = await asyncio.to_thread(
            client._stub.GetGatewayInfo,
            openshell_pb2.GetGatewayInfoRequest(),
            timeout=10,
        )
        version = (info.gateway_version or "").strip()
    except Exception:
        log.debug("GetGatewayInfo RPC failed or not implemented for %s", config.gateway_url, exc_info=True)
        return ""
    if not version:
        return ""

    gateway_url = config.gateway_url.strip()
    row = await db.get(OpenShellGatewayVersion, gateway_url)
    previous_version = row.gateway_version if row else ""
    changed = row is not None and previous_version != version
    if row is None:
        db.add(OpenShellGatewayVersion(gateway_url=gateway_url, gateway_version=version))
    else:
        row.gateway_version = version

    if changed:
        if config.workspace_id is not None:
            gw_result = await db.execute(
                select(WorkspaceGateway.workspace_id).where(
                    WorkspaceGateway.gateway_url == gateway_url
                )
            )
            workspace_ids = list(gw_result.scalars())
            if config.workspace_id not in workspace_ids:
                workspace_ids.append(config.workspace_id)
        else:
            result = await db.execute(
                select(Workspace.id)
                .outerjoin(WorkspaceGateway, WorkspaceGateway.workspace_id == Workspace.id)
                .where(WorkspaceGateway.workspace_id.is_(None))
            )
            workspace_ids = list(result.scalars())
        if workspace_ids:
            await db.execute(
                update(OpencodeSecret)
                .where(OpencodeSecret.workspace_id.in_(workspace_ids))
                .values(
                    gemini_configured=False,
                    openai_configured=False,
                    vertex_configured=False,
                )
            )
        log.warning(
            "OpenShell gateway %s changed version from %s to %s; AI provider "
            "configured flags were reset",
            gateway_url,
            previous_version or "unknown",
            version,
        )

    if config.workspace_id is not None:
        await db.execute(
            update(WorkspaceGateway)
            .where(WorkspaceGateway.gateway_url == gateway_url)
            .values(gateway_version=version)
        )

    await db.flush()
    return version
