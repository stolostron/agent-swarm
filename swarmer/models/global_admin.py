from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from swarmer.database import Base


class GlobalAdmin(Base):
    """A user granted global Swarmer admin rights (ACM-41659).

    Global admins can see and manage every workspace, and can add/remove
    other global admins. This is the primary, self-service way to designate
    admins after the initial deployment bootstrap — it supplements (does not
    replace) the static ``WORKSPACE_ADMIN_USERS`` / ``WORKSPACE_ADMIN_GROUPS``
    env vars, which remain useful for declarative/GitOps-managed admin lists.
    """

    __tablename__ = "global_admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    # K8s ServiceAccount ("system:serviceaccount:<ns>:<name>") or OpenShift
    # OAuth/OIDC username.
    user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Username of the admin who granted this (or "bootstrap" for the
    # zero-admin self-promotion flow).
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
