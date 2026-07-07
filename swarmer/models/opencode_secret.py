from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base


class OpencodeSecret(Base):
    __tablename__ = "opencode_secrets"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_opencode_secrets_workspace_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    shared: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    google_cloud_project: Mapped[str] = mapped_column(Text, nullable=False, default="")
    vertex_location: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Fernet-encrypted JSON content of application_default_credentials.json
    application_default_credentials_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    # Fernet-encrypted Google AI Studio API key
    google_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="opencode_secret"
    )

    # ---------- transparent encrypt/decrypt accessors ----------

    @property
    def application_default_credentials(self) -> str:
        if not self.application_default_credentials_enc:
            return ""
        return crypto.decrypt(self.application_default_credentials_enc)

    @application_default_credentials.setter
    def application_default_credentials(self, value: str) -> None:
        self.application_default_credentials_enc = (
            crypto.encrypt(value) if value else ""
        )

    @property
    def google_api_key(self) -> str:
        if not self.google_api_key_enc:
            return ""
        return crypto.decrypt(self.google_api_key_enc)

    @google_api_key.setter
    def google_api_key(self, value: str) -> None:
        self.google_api_key_enc = crypto.encrypt(value) if value else ""

    # ---------- display helpers (safe to send to browser) ----------

    @property
    def has_adc(self) -> bool:
        return bool(self.application_default_credentials_enc)

    @property
    def has_vertex(self) -> bool:
        return bool(self.google_cloud_project and self.vertex_location)

    @property
    def masked_api_key(self) -> str:
        if not self.google_api_key_enc:
            return ""
        key = self.google_api_key
        if len(key) <= 8:
            return "****"
        return "****" + key[-4:]
