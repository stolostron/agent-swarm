from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from swarmer.database import Base


class OpenShellGatewayVersion(Base):
    """Last observed version for each OpenShell gateway endpoint."""

    __tablename__ = "openshell_gateway_versions"

    gateway_url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    gateway_version: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
