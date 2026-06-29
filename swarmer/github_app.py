"""Resolve workspace GitHub App credentials for session launch."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.models.github_app import GitHubApp


async def get_workspace_github_app(
    workspace_id: int,
    db: AsyncSession,
    user_id: str = "",
) -> GitHubApp | None:
    """Return the configured GitHub App for a workspace, or None.

    Scheduler/queue launches pass an empty user_id and receive the App
    unconditionally (only one row per workspace exists).  User-initiated
    launches prefer the user's own record, then any shared/legacy record.
    Returns None when no fully-configured App exists.
    """
    if not user_id:
        # Scheduler/queue — resolve by workspace_id only.
        result = await db.execute(
            select(GitHubApp).where(GitHubApp.workspace_id == workspace_id)
        )
        app = result.scalar_one_or_none()
        if app is None or not app.is_configured:
            return None
        return app

    result = await db.execute(
        select(GitHubApp).where(
            GitHubApp.workspace_id == workspace_id,
            or_(
                GitHubApp.user_id == user_id,
                GitHubApp.shared == True,  # noqa: E712
                GitHubApp.user_id == "",
            ),
        )
    )
    apps = result.scalars().all()
    app = None
    for candidate in apps:
        if candidate.user_id == user_id:
            app = candidate
            break
    if app is None and apps:
        app = apps[0]
    if app is None or not app.is_configured:
        return None
    return app
