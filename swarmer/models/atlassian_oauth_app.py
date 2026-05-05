from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer import crypto
from swarmer.database import Base


class AtlassianOAuthApp(Base):
    """Per-workspace Atlassian OAuth 2.0 (3LO) configuration.

    Stores the client_id and Fernet-encrypted client_secret from a
    pre-registered Atlassian OAuth app (developer.atlassian.com/console).
    No tokens are persisted here — access tokens live only in the Starlette
    HTTP session and then in an ephemeral K8s Secret for the duration of a
    session pod.
    """

    __tablename__ = "atlassian_oauth_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Display-only: the Atlassian Cloud site the user wants to connect to.
    site_url: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    # OAuth 2.0 app credentials from developer.atlassian.com/console
    client_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    client_secret_enc: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="atlassian_oauth_app"
    )

    @property
    def client_secret(self) -> str:
        if not self.client_secret_enc:
            return ""
        return crypto.decrypt(self.client_secret_enc)

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_enc = crypto.encrypt(value) if value else ""
