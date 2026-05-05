"""
AtlassianToken — per-workspace Atlassian Rovo MCP OAuth token storage.

Stores the dynamically-registered OAuth client credentials and the
access/refresh tokens obtained through the 3LO flow so they can be
injected into agent pods at launch time without requiring the user to
re-authenticate on every launch.
"""
import time
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base

# How many seconds before expiry we consider a token as "needs refresh"
_REFRESH_WINDOW_SECONDS = 300  # 5 minutes


class AtlassianToken(Base):
    __tablename__ = "atlassian_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), unique=True, nullable=False
    )

    # Dynamically-registered OAuth client credentials (encrypted)
    client_id_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    client_id_issued_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # OAuth tokens (Fernet-encrypted at rest)
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Token metadata
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="atlassian_token"
    )

    # ------------------------------------------------------------------
    # Transparent encrypt/decrypt properties
    # ------------------------------------------------------------------

    @property
    def client_id(self) -> str:
        return crypto.decrypt(self.client_id_enc)

    @client_id.setter
    def client_id(self, value: str) -> None:
        self.client_id_enc = crypto.encrypt(value)

    @property
    def access_token(self) -> str:
        return crypto.decrypt(self.access_token_enc)

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.access_token_enc = crypto.encrypt(value)

    @property
    def refresh_token(self) -> str:
        return crypto.decrypt(self.refresh_token_enc)

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self.refresh_token_enc = crypto.encrypt(value)

    # ------------------------------------------------------------------
    # Token lifecycle helpers
    # ------------------------------------------------------------------

    @property
    def is_expired(self) -> bool:
        """Return True if the access token has passed its expiry time."""
        if self.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        # expires_at may be stored as naive UTC from the DB
        exp = (
            self.expires_at
            if self.expires_at.tzinfo is not None
            else self.expires_at.replace(tzinfo=timezone.utc)
        )
        return exp <= now

    @property
    def needs_refresh(self) -> bool:
        """Return True if the access token is expired or expiring within the refresh window."""
        if self.expires_at is None:
            return False
        now_ts = time.time()
        if self.expires_at.tzinfo is not None:
            exp_ts = self.expires_at.timestamp()
        else:
            exp_ts = self.expires_at.replace(tzinfo=timezone.utc).timestamp()
        return exp_ts <= now_ts + _REFRESH_WINDOW_SECONDS

    @property
    def token_status(self) -> str:
        """Return one of: 'connected', 'expiring_soon', 'expired'."""
        if self.is_expired:
            return "expired"
        if self.needs_refresh:
            return "expiring_soon"
        return "connected"
