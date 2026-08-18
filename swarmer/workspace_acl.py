"""Database-backed workspace access control (ACM-41659).

Replaces per-workspace Kubernetes namespace + RoleBinding RBAC now that
OpenShell owns all sandbox lifecycle management — a dedicated K8s namespace
per workspace is no longer required for sandbox execution, only (optionally)
for a handful of legacy per-workspace K8s Secret/ConfigMap features (pull
secrets, extra env vars) which lazily create their namespace on first use.

A user may access a workspace when any of the following hold:
  - ``settings.k8s_namespace`` is set (shared-namespace deployment) — every
    authenticated user can see every workspace, matching the flat trust model
    that deployment flavor already had (a single shared namespace + Role)
  - they are the workspace owner (``Workspace.owner_id``)
  - they have an explicit ``WorkspaceMember`` row for that workspace
  - the workspace is unclaimed (``owner_id`` empty — e.g. pre-ACL workspaces
    with no recoverable owner) — open to any authenticated user until the
    first management action claims it (see ``claim_ownership_if_unowned``)
  - they are a global admin — either statically configured
    (``WORKSPACE_ADMIN_USERS`` / ``WORKSPACE_ADMIN_GROUPS``)
    or granted via the self-service ``global_admins`` table / `/admins` UI

This is a fast local SQL query rather than a per-workspace round-trip to the
Kubernetes API (``SelfSubjectAccessReview``), and does not require Swarmer to
hold cluster-scoped namespace create/list/delete or RoleBinding permissions.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.config import settings
from swarmer.models.global_admin import GlobalAdmin
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_member import WorkspaceMember


def _parse_csv(value: str) -> set[str]:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


def has_static_admin_config() -> bool:
    """True when admins are declared via env vars (WORKSPACE_ADMIN_USERS/GROUPS)."""
    return bool(_parse_csv(settings.workspace_admin_users) or _parse_csv(settings.workspace_admin_groups))


def is_admin_static(username: str, groups: list[str] | None = None) -> bool:
    """Return True when *username*/*groups* match the env-var admin allow-list.

    This is the config-only (no DB) building block of `is_admin()`, exposed
    separately for the rare call sites that can't await a DB query.
    """
    if username and username in _parse_csv(settings.workspace_admin_users):
        return True
    admin_groups = _parse_csv(settings.workspace_admin_groups)
    if admin_groups and groups and admin_groups.intersection(groups):
        return True
    return False


async def is_admin(db: AsyncSession, username: str, groups: list[str] | None = None) -> bool:
    """Return True when *username* is a global admin.

    Checks the static env-var allow-list first (no query), then the
    self-service `global_admins` table. Admins can see and manage every
    workspace and manage other admins.
    """
    if is_admin_static(username, groups):
        return True
    if not username:
        return False
    result = await db.execute(select(GlobalAdmin.id).where(GlobalAdmin.user_id == username))
    return result.scalar_one_or_none() is not None


async def admin_bootstrap_available(db: AsyncSession) -> bool:
    """True when no admin exists yet (static or DB) — the very first
    logged-in user may self-promote via the one-click "Become Admin" flow."""
    if has_static_admin_config():
        return False
    result = await db.execute(select(func.count()).select_from(GlobalAdmin))
    return (result.scalar_one() or 0) == 0


async def bootstrap_admin(db: AsyncSession, username: str) -> bool:
    """Self-promote *username* to global admin iff no admin exists yet.

    Returns True on success, False if an admin already exists (races are
    resolved by the DB — a second caller sees admin_bootstrap_available()
    become False, or hits the unique constraint on user_id / a concurrent
    commit and simply fails the initial check).
    """
    if not username or not await admin_bootstrap_available(db):
        return False
    db.add(GlobalAdmin(user_id=username, created_by="bootstrap"))
    await db.commit()
    return True


async def list_global_admins(db: AsyncSession) -> list[GlobalAdmin]:
    result = await db.execute(select(GlobalAdmin).order_by(GlobalAdmin.user_id))
    return list(result.scalars().all())


async def add_global_admin(db: AsyncSession, user_id: str, created_by: str) -> GlobalAdmin:
    admin = GlobalAdmin(user_id=user_id, created_by=created_by)
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def remove_global_admin(db: AsyncSession, user_id: str) -> bool:
    result = await db.execute(select(GlobalAdmin).where(GlobalAdmin.user_id == user_id))
    admin = result.scalar_one_or_none()
    if admin is None:
        return False
    await db.delete(admin)
    await db.commit()
    return True


async def can_create_workspace(
    db: AsyncSession, username: str, groups: list[str] | None = None
) -> bool:
    """Return True when *username* is allowed to create new workspaces."""
    if not username:
        return False
    policy = (settings.workspace_create_policy or "all").strip().lower()
    if policy == "admins":
        return await is_admin(db, username, groups)
    return True


async def user_can_access_workspace(
    db: AsyncSession, ws: Workspace, username: str, groups: list[str] | None = None
) -> bool:
    """Return True when *username* may view/manage *ws*."""
    if not username:
        return False
    if settings.k8s_namespace:
        # Shared-namespace deployment: all workspaces already live in one
        # namespace with one Role/RoleBinding — preserve that flat access
        # model rather than requiring per-workspace membership.
        return True
    if await is_admin(db, username, groups):
        return True
    if ws.owner_id == username:
        return True
    # Unclaimed workspace (no owner recorded — e.g. a pre-ACL workspace with
    # no recoverable owner): open to any authenticated user until claimed.
    if not ws.owner_id:
        return True
    result = await db.execute(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == username,
        )
    )
    return result.scalar_one_or_none() is not None


async def filter_accessible_workspaces(
    db: AsyncSession,
    workspaces: list[Workspace],
    username: str,
    groups: list[str] | None = None,
) -> list[Workspace]:
    """Return the subset of *workspaces* accessible to *username*."""
    if not workspaces or not username:
        return []
    if settings.k8s_namespace:
        return list(workspaces)
    if await is_admin(db, username, groups):
        return list(workspaces)
    accessible_ids = {ws.id for ws in workspaces if ws.owner_id == username or not ws.owner_id}
    result = await db.execute(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == username,
            WorkspaceMember.workspace_id.in_([ws.id for ws in workspaces]),
        )
    )
    accessible_ids |= {row[0] for row in result.all()}
    return [ws for ws in workspaces if ws.id in accessible_ids]


async def can_manage_members(
    db: AsyncSession, ws: Workspace, username: str, groups: list[str] | None = None
) -> bool:
    """Owner, a global admin, or anyone (while the workspace is unclaimed)
    can rename/delete a workspace or manage its members."""
    if not username:
        return False
    if await is_admin(db, username, groups):
        return True
    if not ws.owner_id:
        return True
    return ws.owner_id == username


async def _db_known_users(
    db: AsyncSession, username: str, groups: list[str] | None = None
) -> set[str]:
    """DB-known usernames visible to *username* (owners/members of
    workspaces), never a global directory for non-admins.

    - Global admins see every known username in the system (owners/members of
      every workspace, plus other admins) — they already have that
      visibility via the full workspace list and `/admins`.
    - Everyone else sees only usernames that already share a workspace with
      them (owners + members of workspaces they own or belong to) — "people
      I already work with," not an arbitrary enumeration of every user who
      has ever logged into Swarmer.
    """
    if await is_admin(db, username, groups):
        owner_rows = await db.execute(
            select(Workspace.owner_id).where(Workspace.owner_id != "")
        )
        member_rows = await db.execute(select(WorkspaceMember.user_id))
        admin_rows = await db.execute(select(GlobalAdmin.user_id))
        return (
            {row[0] for row in owner_rows.all()}
            | {row[0] for row in member_rows.all()}
            | {row[0] for row in admin_rows.all()}
        )

    owned_result = await db.execute(
        select(Workspace.id).where(Workspace.owner_id == username)
    )
    member_of_result = await db.execute(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == username)
    )
    ws_ids = {row[0] for row in owned_result.all()} | {row[0] for row in member_of_result.all()}
    if not ws_ids:
        return set()
    owner_rows = await db.execute(
        select(Workspace.owner_id).where(
            Workspace.id.in_(ws_ids), Workspace.owner_id != ""
        )
    )
    member_rows = await db.execute(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id.in_(ws_ids))
    )
    return {row[0] for row in owner_rows.all()} | {row[0] for row in member_rows.all()}


async def _k8s_known_users() -> set[str]:
    """OpenShift `User` objects + K8s ServiceAccounts (the identities
    `make user-token SA_USER=<name>` creates) — best-effort, never raises."""
    import asyncio

    from swarmer import k8s

    openshift_users, service_accounts = await asyncio.gather(
        asyncio.to_thread(k8s.list_openshift_users),
        asyncio.to_thread(k8s.list_user_service_accounts),
    )
    return set(openshift_users) | set(service_accounts)


async def list_known_users(
    db: AsyncSession, username: str, groups: list[str] | None = None
) -> list[str]:
    """Return usernames suggested to *username* for the Add Member / Add
    Admin autocomplete — never a global user directory.

    Merges three sources:
      - DB-known users (see `_db_known_users` — visibility-scoped for
        non-admins, every known user for admins)
      - OpenShift `User` objects (cluster-scoped; empty if not on OpenShift)
      - K8s ServiceAccounts `make user-token SA_USER=<name>` would create,
        formatted as `system:serviceaccount:<ns>:<name>`

    Free-text entry is always still allowed on the calling forms — this only
    powers suggestions, it never restricts who can actually be granted access
    (that's still enforced by `can_manage_members` / auth at request time).
    """
    if not username:
        return []

    db_users = await _db_known_users(db, username, groups)
    k8s_users = await _k8s_known_users()

    users = db_users | k8s_users
    users.discard(username)
    return sorted(users)


def claim_ownership_if_unowned(ws: Workspace, username: str) -> bool:
    """If *ws* has no owner yet, claim it for *username* (caller must have
    already confirmed `can_manage_members`). Returns True if a claim happened.

    Call this right before executing a management action (rename, delete,
    add/remove member) on an unclaimed workspace so the first person to act
    on it becomes its owner going forward — nothing stays ownerless forever.
    This only mutates the in-memory `ws.owner_id` — the caller's subsequent
    `db.commit()` persists it alongside the actual management action.
    """
    if ws.owner_id or not username:
        return False
    ws.owner_id = username
    return True
