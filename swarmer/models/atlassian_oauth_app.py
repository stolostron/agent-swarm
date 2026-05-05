from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base


class AtlassianOAuthApp(Base):
    """Per-workspace Atlassian OAuth configuration.

    Stores only non-secret configuration: the display site URL and the
    redirect URI derived from SWARMER_PUBLIC_URL.  No client ID/secret is
    persisted here — the Rovo MCP Server uses Dynamic Client Registration
    (DCR) and swarmer registers a new client on each OAuth flow start.
    """

    __tablename__ = "atlassian_oauth_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Display-only: the Atlassian Cloud site the user wants to connect to.
    # Not used in the OAuth flow itself (the Rovo MCP endpoint is global).
    site_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # The redirect URI registered with Atlassian during DCR.
    # Derived from SWARMER_PUBLIC_URL at save time.
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="atlassian_oauth_app"
    )
