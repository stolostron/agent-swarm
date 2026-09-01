from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.crypto import decrypt, encrypt
from swarmer.database import Base


class WorkspaceGateway(Base):
    __tablename__ = "workspace_gateways"

    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    gateway_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="oidc")
    # auth_mode values: "oidc", "bearer", "mtls", "none"

    # OIDC configuration and encrypted tokens
    oidc_issuer: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    oidc_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_audience: Mapped[str | None] = mapped_column(String(255), nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Bearer token (static / API key)
    bearer_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # mTLS certificates
    tls_ca: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        "Workspace", back_populates="gateway"
    )

    @property
    def refresh_token(self) -> str:
        return decrypt(self.refresh_token_enc) if self.refresh_token_enc else ""

    @refresh_token.setter
    def refresh_token(self, value: str | None) -> None:
        self.refresh_token_enc = encrypt(value) if value else None

    @property
    def access_token(self) -> str:
        return decrypt(self.access_token_enc) if self.access_token_enc else ""

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self.access_token_enc = encrypt(value) if value else None

    @property
    def bearer_token(self) -> str:
        return decrypt(self.bearer_token_enc) if self.bearer_token_enc else ""

    @bearer_token.setter
    def bearer_token(self, value: str | None) -> None:
        self.bearer_token_enc = encrypt(value) if value else None

    @property
    def tls_key(self) -> str:
        return decrypt(self.tls_key_enc) if self.tls_key_enc else ""

    @tls_key.setter
    def tls_key(self, value: str | None) -> None:
        self.tls_key_enc = encrypt(value) if value else None
