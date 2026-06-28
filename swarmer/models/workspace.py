from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    opencode_secret: Mapped["OpencodeSecret"] = relationship(  # noqa: F821
        back_populates="workspace", uselist=False, cascade="all, delete-orphan"
    )
    github_pats: Mapped[list["GitHubPAT"]] = relationship(  # noqa: F821
        back_populates="workspace", cascade="all, delete-orphan"
    )
    github_app: Mapped["GitHubApp | None"] = relationship(  # noqa: F821
        back_populates="workspace", uselist=False, cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        back_populates="workspace", cascade="all, delete-orphan"
    )
    mcp_servers: Mapped[list["McpServer"]] = relationship(  # noqa: F821
        back_populates="workspace", cascade="all, delete-orphan"
    )
    prompt_sources: Mapped[list["WorkspacePromptSource"]] = relationship(  # noqa: F821
        "WorkspacePromptSource", back_populates="workspace", cascade="all, delete-orphan"
    )
    env_vars: Mapped[list["SandboxEnvVar"]] = relationship(  # noqa: F821
        "SandboxEnvVar", back_populates="workspace", cascade="all, delete-orphan"
    )
    github_app: Mapped["GitHubApp | None"] = relationship(  # noqa: F821
        "GitHubApp", back_populates="workspace", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def k8s_namespace(self) -> str:
        from swarmer.config import settings
        return settings.k8s_namespace or self.namespace
