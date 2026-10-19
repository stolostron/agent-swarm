from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
    # Fernet-encrypted Google AI Studio API key.
    # LEGACY (ACM-37263): the secrets UI no longer writes to this column — new
    # keys are pushed directly to the OpenShell gateway provider
    # (swarmer-ws-{id}-google-ai-studio) and never stored in the Swarmer DB,
    # mirroring the ADC pattern. This column is retained only so previously
    # saved keys keep working (via the accessors below) until rotated.
    google_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    openai_api_key_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    gemini_configured: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    openai_configured: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    vertex_configured: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    # EnMaaS (ACM-48198): non-secret marker that the workspace EnMaaS provider
    # was configured on the OpenShell gateway. There is deliberately NO token
    # column — the ENMAAS_KEY credential is gateway-only, mirroring the OpenAI
    # flow, so the flag is the sole active Swarmer-side state for the key.
    enmaas_configured: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    # Encrypted workspace EnMaaS gateway URL, entered in the AI Tokens form.
    # Only workspace managers may set it. It drives provider registration,
    # generated OpenCode config, and sandbox egress; the token itself stays
    # gateway-only.
    enmaas_base_url_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
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

    @property
    def has_gemini(self) -> bool:
        return self.gemini_configured or bool(self.google_api_key_enc)

    @property
    def has_openai(self) -> bool:
        return self.openai_configured or bool(self.openai_api_key_enc)

    @property
    def has_enmaas(self) -> bool:
        """Report the gateway credential marker without retrieving its token."""
        # Marker only — the token itself lives on the OpenShell gateway.
        return self.enmaas_configured

    @property
    def enmaas_base_url(self) -> str:
        """Decrypt the stored workspace EnMaaS endpoint, if one is present."""
        if not self.enmaas_base_url_enc:
            return ""
        return crypto.decrypt(self.enmaas_base_url_enc)

    @enmaas_base_url.setter
    def enmaas_base_url(self, value: str) -> None:
        """Encrypt a workspace EnMaaS endpoint before storing it."""
        self.enmaas_base_url_enc = crypto.encrypt(value) if value else ""


    # ---------- display helpers (safe to send to browser) ----------

    @property
    def has_adc(self) -> bool:
        return bool(self.application_default_credentials_enc)

    @property
    def has_vertex(self) -> bool:
        return self.vertex_configured or bool(
            self.application_default_credentials_enc
            and self.google_cloud_project
            and self.vertex_location
        )

    @property
    def masked_api_key(self) -> str:
        if not self.google_api_key_enc:
            return ""
        key = self.google_api_key
        if len(key) <= 8:
            return "****"
        return "****" + key[-4:]


async def workspace_enmaas_base_url(
    db: "AsyncSession",
    ws_id: int,
    prefer: "OpencodeSecret | None" = None,
) -> str:
    """Resolve a validated EnMaaS URL from this workspace's credential rows.

    The URL is workspace-scoped and may be stored on a manager's shared
    credential row while another member's row is selected for the session.
    Check *prefer* first, then other rows, returning an empty string if none
    contains a valid URL.
    """
    from sqlalchemy import select

    from swarmer.config import sanitize_enmaas_base_url

    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    rows = list(result.scalars().all())
    ordered = ([prefer] if prefer is not None else []) + [
        row for row in rows if row is not prefer
    ]
    for row in ordered:
        url = sanitize_enmaas_base_url(row.enmaas_base_url or "")
        if url:
            return url
    return ""
