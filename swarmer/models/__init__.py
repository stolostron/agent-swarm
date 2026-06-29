# Import all models here so Base.metadata knows about every table.
from swarmer.models.workspace import Workspace  # noqa: F401
from swarmer.models.opencode_secret import OpencodeSecret  # noqa: F401
from swarmer.models.github_pat import GitHubPAT  # noqa: F401
from swarmer.models.github_app import GitHubApp  # noqa: F401
from swarmer.models.session import Session  # noqa: F401
from swarmer.models.session_repo import SessionRepo  # noqa: F401
from swarmer.models.session_run import SessionRun  # noqa: F401
from swarmer.models.mcp_server import McpServer  # noqa: F401
from swarmer.models.workspace_prompt import WorkspacePromptSource, WorkspacePrompt  # noqa: F401
from swarmer.models.sandbox_env_var import SandboxEnvVar  # noqa: F401
from swarmer.models.session_schedule import SessionSchedule  # noqa: F401
