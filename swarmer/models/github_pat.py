from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import swarmer.crypto as crypto
from swarmer.database import Base


class GitHubPAT(Base):
    __tablename__ = "github_pats"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    shared: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    github_username: Mapped[str] = mapped_column(Text, nullable=False)
    github_org: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pat_enc: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="github_pats"
    )
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        back_populates="github_pat"
    )

    @property
    def pat(self) -> str:
        return crypto.decrypt(self.pat_enc)

    @pat.setter
    def pat(self, value: str) -> None:
        self.pat_enc = crypto.encrypt(value)

    @property
    def k8s_secret_name(self) -> str:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return f"github-pat-{slug}"
