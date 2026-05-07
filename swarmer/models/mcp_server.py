from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base


class McpServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    server_url: Mapped[str] = mapped_column(Text, nullable=False)
    server_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="http", server_default="http"
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="1"
    )

    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Jira API token fields
    jira_server_url: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    jira_access_token_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    jira_email: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="mcp_servers"
    )

    # ---------- transparent encrypt/decrypt accessors ----------

    @property
    def jira_access_token(self) -> str:
        if not self.jira_access_token_enc:
            return ""
        return crypto.decrypt(self.jira_access_token_enc)

    @jira_access_token.setter
    def jira_access_token(self, value: str) -> None:
        self.jira_access_token_enc = crypto.encrypt(value) if value else ""

    # ---------- display helpers ----------

    @property
    def is_authenticated(self) -> bool:
        return bool(self.jira_access_token_enc)

    @property
    def auth_status(self) -> str:
        if not self.is_authenticated:
            return "not_configured"
        return "expired" if self.token_expires_at else "active"

    @property
    def auth_status_label(self) -> str:
        labels = {
            "not_configured": "Not Connected",
            "expired": "Token Expired",
            "active": "Connected",
        }
        return labels.get(self.auth_status, "Unknown")

    @property
    def auth_status_color(self) -> str:
        colors = {
            "not_configured": "grey",
            "expired": "gold",
            "active": "green",
        }
        return colors.get(self.auth_status, "grey")
