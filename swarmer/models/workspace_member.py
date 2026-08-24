from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base


class WorkspaceMember(Base):
    """Explicit per-user grant of access to a workspace (ACM-41659).

    Replaces per-workspace Kubernetes namespace + RoleBinding RBAC now that
    OpenShell owns all sandbox lifecycle management — workspace access is a
    purely application-level (database-backed) concept. A user can access a
    workspace if they are the owner (``Workspace.owner_id``), have an explicit
    ``WorkspaceMember`` row here, or are a configured workspace admin (see
    ``swarmer.workspace_acl``).
    """

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    # K8s username (ServiceAccount "system:serviceaccount:<ns>:<name>" or
    # OpenShift OAuth/OIDC username) granted access to this workspace.
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="member", server_default="member"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="members"
    )
