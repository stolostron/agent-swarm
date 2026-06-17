"""Pydantic request/response schemas for the REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ============================================================
# Workspaces
# ============================================================


class WorkspaceCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class WorkspaceUpdate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class WorkspaceOut(BaseModel):
    id: int
    display_name: str
    namespace: str
    description: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Sessions
# ============================================================


class SessionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    mode: str = Field("prompt", pattern=r"^(tui|server|prompt)$")
    model: str = ""
    agent_tool: str = "opencode"
    instruction_prompt: str = ""
    github_pat_id: int | None = None
    prompt_id: int | None = None
    persist: bool = False
    working_branch: str = ""
    mcp_server_ids: list[int] = Field(default_factory=list)


class SessionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    mode: str | None = Field(None, pattern=r"^(tui|server|prompt)$")
    model: str | None = None
    agent_tool: str | None = None
    instruction_prompt: str | None = None
    github_pat_id: int | None = None
    prompt_id: int | None = None
    persist: bool | None = None
    working_branch: str | None = None
    mcp_server_ids: list[int] | None = None


class SessionOut(BaseModel):
    id: int
    workspace_id: int
    name: str
    mode: str
    model: str
    agent_tool: str
    instruction_prompt: str
    github_pat_id: int | None
    prompt_id: int | None
    persist: bool
    privileged: bool
    working_branch: str
    phase: str
    status_detail: str
    pod_name: str | None
    pvc_name: str | None
    cron_schedule: str
    cron_label: str
    run_started_at: datetime | None
    run_completed_at: datetime | None
    run_duration: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionOutput(BaseModel):
    output: str


class SessionRunOut(BaseModel):
    id: int
    session_id: int
    phase: str
    status_detail: str
    started_at: datetime
    completed_at: datetime
    run_duration: str
    last_output: str

    model_config = {"from_attributes": True}


class ScheduleRequest(BaseModel):
    cron_expr: str = Field(..., min_length=1, max_length=128)


class SetNameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class SetModeRequest(BaseModel):
    mode: str = Field(..., pattern=r"^(tui|server|prompt)$")


class SetModelRequest(BaseModel):
    model: str = ""


class PatchResult(BaseModel):
    patch: str
    commit_msg: str
    filename: str


# ============================================================
# Repos
# ============================================================


class RepoCreate(BaseModel):
    repo_url: str
    branch: str = "main"
    local_path: str = ""


class RepoOut(BaseModel):
    id: int
    session_id: int
    repo_url: str
    branch: str
    local_path: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Secrets / Credentials
# ============================================================


class CredentialsSave(BaseModel):
    google_cloud_project: str = ""
    vertex_location: str = ""
    google_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # Raw JSON from gcloud application-default login (or service account key)
    application_default_credentials: str = ""
    shared: bool = False


class CredentialsOut(BaseModel):
    id: int
    workspace_id: int
    has_adc: bool
    has_vertex: bool
    has_anthropic: bool
    has_openai: bool
    google_cloud_project: str
    vertex_location: str
    masked_api_key: str
    masked_anthropic_key: str
    masked_openai_key: str
    shared: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# GitHub PATs
# ============================================================


class PATCreate(BaseModel):
    name: str = Field(..., min_length=1)
    github_username: str = Field(..., min_length=1)
    github_org: str = ""
    pat_value: str = Field(..., min_length=1)
    description: str = ""
    shared: bool = False


class PATUpdate(BaseModel):
    name: str | None = None
    github_username: str | None = None
    github_org: str | None = None
    pat_value: str | None = None
    description: str | None = None
    shared: bool | None = None


class PATOut(BaseModel):
    id: int
    workspace_id: int
    name: str
    github_username: str
    github_org: str
    description: str
    shared: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# GitHub App (workspace installation)
# ============================================================


class GitHubAppSave(BaseModel):
    app_id: str = Field(..., min_length=1)
    installation_id: str = Field(..., min_length=1)
    private_key: str = ""
    shared: bool = False


class GitHubAppOut(BaseModel):
    id: int
    workspace_id: int
    app_id: str
    installation_id: str
    has_private_key: bool
    shared: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Pull Secret
# ============================================================


class PullSecretCreate(BaseModel):
    registry: str = "quay.io"
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class PullSecretOut(BaseModel):
    exists: bool
    registry: str | None = None


# ============================================================
# Environment Variables
# ============================================================


class EnvVarCreate(BaseModel):
    key: str = Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
    value: str


class EnvVarOut(BaseModel):
    key: str
    value: str


# ============================================================
# MCP Servers
# ============================================================


class McpServerAddFromCatalog(BaseModel):
    catalog_slug: str


class McpServerSaveConfig(BaseModel):
    jira_server_url: str
    jira_access_token: str = ""
    jira_email: str


class McpServerOut(BaseModel):
    id: int
    workspace_id: int
    slug: str
    display_name: str
    server_url: str
    server_type: str
    enabled: bool
    auth_status: str
    auth_status_label: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class McpHealthOut(BaseModel):
    statuses: dict[str, dict[str, str]]


# ============================================================
# Prompt Sources & Prompts
# ============================================================


class PromptSourceCreate(BaseModel):
    name: str = Field(..., min_length=1)
    github_pat_id: int | None = None
    repo_url: str = Field(..., min_length=1)
    branch: str = "main"
    folder_path: str = "."


class PromptSourceUpdate(BaseModel):
    name: str | None = None
    github_pat_id: int | None = None
    repo_url: str | None = None
    branch: str | None = None
    folder_path: str | None = None


class PromptOut(BaseModel):
    id: int
    filename: str
    display_name: str
    content: str
    content_hash: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PromptSourceOut(BaseModel):
    id: int
    workspace_id: int
    name: str
    github_pat_id: int | None
    repo_url: str
    branch: str
    folder_path: str
    last_synced_at: datetime | None
    sync_error: str
    prompts: list[PromptOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Generic
# ============================================================


class MessageOut(BaseModel):
    detail: str
