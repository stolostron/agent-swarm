"""Startup migration helper (ACM-41659 follow-up).

Mirrors legacy K8s RBAC workspace grants into the database-backed ACL so
nobody has to be manually re-added to a workspace they already had access to
before Swarmer moved away from per-workspace namespace + RoleBinding RBAC.

The bulk of the migration (backfilling `workspace_members` and
`Workspace.owner_id` from existing per-user credential tables) runs as plain
SQL in `database.py:migrate_db()`. This module covers the one thing that
can't be expressed in SQL: reading K8s RoleBindings created by the historical
`make grant-workspace-access` flow. It is best-effort and never raises —
any K8s error is logged and skipped so it can never block startup.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.config import settings
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_member import WorkspaceMember

log = logging.getLogger(__name__)


async def sync_k8s_workspace_members(db: AsyncSession) -> None:
    """Mirror `swarmer-user` RoleBinding grants into `workspace_members`.

    No-op in shared-namespace deployments (`settings.k8s_namespace` set) —
    those already grant every authenticated user access to every workspace
    (see `workspace_acl.py`), so there is nothing to migrate per-workspace.
    """
    if settings.k8s_namespace:
        return

    result = await db.execute(select(Workspace))
    workspaces = result.scalars().all()
    if not workspaces:
        return

    changed = False
    for ws in workspaces:
        try:
            identities = k8s.list_swarmer_user_role_binding_identities(ws.k8s_namespace)
        except Exception:
            log.warning(
                "K8s workspace-member sync failed for workspace %s (%s)",
                ws.id, ws.k8s_namespace, exc_info=True,
            )
            continue
        if not identities:
            continue

        existing = await db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == ws.id)
        )
        existing_ids = {row[0] for row in existing.all()}

        for user_id in identities:
            if user_id == ws.owner_id or user_id in existing_ids:
                continue
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=user_id, role="member"))
            existing_ids.add(user_id)
            changed = True

        if not ws.owner_id:
            ws.owner_id = identities[0]
            changed = True

    if changed:
        await db.commit()
        log.info("K8s workspace-member sync: migrated legacy swarmer-user RoleBinding grants into workspace_members")
