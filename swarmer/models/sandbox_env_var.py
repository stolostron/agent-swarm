from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base


class SandboxEnvVar(Base):
    __tablename__ = "workspace_env_vars"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet-encrypted environment variable value
    value_enc: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="env_vars"
    )

    # ---------- transparent encrypt/decrypt accessor ----------

    @property
    def value(self) -> str:
        if not self.value_enc:
            return ""
        return crypto.decrypt(self.value_enc)

    @value.setter
    def value(self, plaintext: str) -> None:
        self.value_enc = crypto.encrypt(plaintext) if plaintext else ""
