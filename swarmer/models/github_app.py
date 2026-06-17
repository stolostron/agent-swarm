from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base


class GitHubApp(Base):
    """Workspace GitHub App installation credentials for session pods."""

    __tablename__ = "github_apps"
    __table_args__ = (UniqueConstraint("workspace_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    shared: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    app_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    installation_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    private_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="github_app")  # noqa: F821

    @property
    def private_key(self) -> str:
        if not self.private_key_enc:
            return ""
        return crypto.decrypt(self.private_key_enc)

    @private_key.setter
    def private_key(self, value: str) -> None:
        self.private_key_enc = crypto.encrypt(value) if value else ""

    @property
    def is_configured(self) -> bool:
        return bool(
            self.app_id.strip()
            and self.installation_id.strip()
            and self.private_key_enc
        )

    @property
    def k8s_secret_name(self) -> str:
        return "github-app"
