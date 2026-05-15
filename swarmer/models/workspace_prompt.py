from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base

if TYPE_CHECKING:
    from swarmer.models.workspace import Workspace
    from swarmer.models.github_pat import GitHubPAT
    from swarmer.models.session import Session


class WorkspacePromptSource(Base):
    __tablename__ = "workspace_prompt_sources"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    github_pat_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("github_pats.id"), nullable=True
    )
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False, default="main", server_default="main")
    folder_path: Mapped[str] = mapped_column(Text, nullable=False, default=".", server_default=".")
    
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="prompt_sources")
    github_pat: Mapped["GitHubPAT | None"] = relationship()
    prompts: Mapped[list["WorkspacePrompt"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class WorkspacePrompt(Base):
    __tablename__ = "workspace_prompts"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspace_prompt_sources.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    source: Mapped["WorkspacePromptSource"] = relationship(back_populates="prompts")
    sessions: Mapped[list["Session"]] = relationship(back_populates="prompt")
