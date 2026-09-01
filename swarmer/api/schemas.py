"""Pydantic request/response schemas for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ============================================================
# Workspaces
# ============================================================


class WorkspaceGatewayCreate(BaseModel):
    gateway_url: str = Field(..., min_length=1, max_length=1024)
    auth_mode: str = Field("oidc", max_length=32)
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    refresh_token: str | None = None
    access_token: str | None = None
    bearer_token: str | None = None
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_verify: bool = True


class WorkspaceGatewayOut(BaseModel):
    workspace_id: int
    gateway_url: str
    auth_mode: str
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    has_refresh_token: bool = False
    has_access_token: bool = False
    access_token_expires_at: datetime | None = None
    has_bearer_token: bool = False
    has_tls_cert: bool = False
    has_tls_key: bool = False
    tls_ca: str | None = None
    tls_verify: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ParseGatewayCommandIn(BaseModel):
    command: str = Field(..., min_length=1)


class ParseGatewayCommandOut(BaseModel):
    gateway_url: str = ""
    auth_mode: str = "oidc"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    bearer_token: str | None = None
    tls_verify: bool = True
    suggested_name: str | None = None
    errors: list[str] = Field(default_factory=list)


class ParseTokenIn(BaseModel):
    token_input: str = Field(..., min_length=1)


class ParseTokenOut(BaseModel):
    refresh_token: str = ""
    access_token: str = ""
    expires_at: int | None = None
    issuer: str | None = None
    client_id: str | None = None
    format_detected: str = "raw"
    status: str = "valid"
    message: str = ""
    char_count: int = 0


class TestGatewayConnectionIn(BaseModel):
    workspace_id: int | None = None
    gateway_url: str = Field(..., min_length=1)
    auth_mode: str = "oidc"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    refresh_token: str | None = None
    bearer_token: str | None = None
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_verify: bool = True


class TestGatewayConnectionOut(BaseModel):
    status: str = "ok"
    gateway_url: str = ""
    auth_mode: str = ""
    sandboxes_count: int = 0
    detail: str = ""


class WorkspaceCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    gateway: WorkspaceGatewayCreate | None = None


class WorkspaceUpdate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class WorkspaceOut(BaseModel):
    id: int
    display_name: str
    namespace: str
    description: str
    owner_id: str = ""
    gateway: WorkspaceGatewayOut | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Workspace Members (ACM-41659) — database-backed workspace ACL
# ============================================================


class WorkspaceMemberCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    role: str = "member"


class WorkspaceMemberOut(BaseModel):
    id: int
    workspace_id: int
    user_id: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Global Admins (ACM-41659) — simple self-service admin flow
# ============================================================


class GlobalAdminCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)


class GlobalAdminOut(BaseModel):
    id: int
    user_id: str
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MeOut(BaseModel):
    username: str
    is_admin: bool
    can_create_workspace: bool
    admin_bootstrap_available: bool


class KnownUsersOut(BaseModel):
    users: list[str]


# ============================================================
# Session Schedules (defined before SessionOut to avoid forward reference)
# ============================================================


class ScheduleEntryCreate(BaseModel):
    trigger_type: str = Field("cron", pattern=r"^(cron|event)$")
    cron_schedule: str = Field("", max_length=128)
    event_condition: str = Field("", max_length=64, pattern=r"^(|ci_fail_or_conflict|new_pr_or_commit|review_comments|any_actionable)$")
    author_scope: str = Field("all", max_length=32, pattern=r"^(self|team|bots|all)$")
    fix_authors: str = Field("", max_length=512)
    label: str = ""
    prompt_id: int
    instruction_prompt: str = ""
    include_event_context: bool = True
    enabled: bool = True


class ScheduleEntryUpdate(BaseModel):
    trigger_type: str | None = Field(None, pattern=r"^(cron|event)$")
    cron_schedule: str | None = Field(None, max_length=128)
    event_condition: str | None = Field(None, max_length=64, pattern=r"^(|ci_fail_or_conflict|new_pr_or_commit|review_comments|any_actionable)$")
    author_scope: str | None = Field(None, max_length=32, pattern=r"^(self|team|bots|all)$")
    fix_authors: str | None = Field(None, max_length=512)
    label: str | None = None
    prompt_id: int | None = None
    instruction_prompt: str | None = None
    include_event_context: bool | None = None
    enabled: bool | None = None


class ScheduleEntryOut(BaseModel):
    id: int
    session_id: int
    trigger_type: str = "cron"
    event_condition: str = ""
    author_scope: str = "all"
    fix_authors: str = ""
    cron_schedule: str
    cron_next_run: datetime | None
    label: str
    prompt_id: int | None
    instruction_prompt: str
    include_event_context: bool = True
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# Sessions
# ============================================================


class SessionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    mode: str = Field("prompt", pattern=r"^(tui|server|prompt)$")
    provider: str = ""
    agent_tool: str = "opencode"
    instruction_prompt: str = ""
    github_pat_id: int | None = None
    prompt_id: int | None = None
    working_branch: str = ""
    mcp_server_ids: list[int] = Field(default_factory=list)


class SessionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    mode: str | None = Field(None, pattern=r"^(tui|server|prompt)$")
    provider: str | None = None
    agent_tool: str | None = None
    instruction_prompt: str | None = None
    github_pat_id: int | None = None
    prompt_id: int | None = None
    working_branch: str | None = None
    mcp_server_ids: list[int] | None = None


class SessionOut(BaseModel):
    id: int
    workspace_id: int
    name: str
    mode: str
    provider: str
    agent_tool: str
    instruction_prompt: str
    github_pat_id: int | None
    prompt_id: int | None
    working_branch: str
    phase: str
    status_detail: str
    sandbox_name: str | None = None
    service_url: str | None = None
    # Deprecated schedule fields — kept for backward compatibility; no new writes.
    cron_schedule: str
    cron_label: str
    run_started_at: datetime | None
    run_completed_at: datetime | None
    run_duration: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    schedules: list["ScheduleEntryOut"] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class SessionOutput(BaseModel):
    output: str
    raw_output: str = ""


class SessionRunOut(BaseModel):
    id: int
    session_id: int
    phase: str
    status_detail: str
    started_at: datetime
    completed_at: datetime
    run_duration: str
    last_output: str
    raw_output: str = ""
    schedule_label: str = ""
    prompt_name: str = ""
    mode: str = "prompt"
    trigger_type: str = "manual"
    event_context: str = ""

    model_config = {"from_attributes": True}


class SessionLaunchRequest(BaseModel):
    pr_context: dict[str, Any] | None = None
    event_context: str | None = None
    instruction_prompt: str | None = None


class ScheduleRequest(BaseModel):
    cron_expr: str = Field(..., min_length=1, max_length=128)


class SetNameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class SetModeRequest(BaseModel):
    mode: str = Field(..., pattern=r"^(tui|server|prompt)$")


class SetProviderRequest(BaseModel):
    provider: str = ""


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
    openai_api_key: str = ""
    application_default_credentials: str = ""
    shared: bool = False


class CredentialsOut(BaseModel):
    id: int
    workspace_id: int
    has_adc: bool
    has_vertex: bool
    google_cloud_project: str
    vertex_location: str
    masked_api_key: str
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
    user_id: str
    name: str
    github_username: str
    github_org: str
    description: str
    shared: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# GitHub App
# ============================================================


class GitHubAppSave(BaseModel):
    app_id: str = Field(..., min_length=1)
    installation_id: str = Field(..., min_length=1)
    private_key: str = ""  # omit to keep existing key on update
    shared: bool = False


class GitHubAppOut(BaseModel):
    id: int
    workspace_id: int
    app_id: str
    installation_id: str
    has_private_key: bool  # never exposes the raw PEM
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
