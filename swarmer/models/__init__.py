# Import all models here so Base.metadata knows about every table.
from swarmer.models.workspace import Workspace  # noqa: F401
from swarmer.models.opencode_secret import OpencodeSecret  # noqa: F401
from swarmer.models.github_pat import GitHubPAT  # noqa: F401
from swarmer.models.session import Session  # noqa: F401
from swarmer.models.session_repo import SessionRepo  # noqa: F401
from swarmer.models.atlassian_oauth_app import AtlassianOAuthApp  # noqa: F401
