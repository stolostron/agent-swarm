# Swarmer Specifications

This directory documents the persisted domain model, versioned REST API, and
Agent Swarm MCP server contract.

## Documents

- [Data Model](data-model.md): SQLite entities, relationships, lifecycle state,
  retention, encryption, and invariants.
- [REST API](rest-api.md): authentication, authorization, endpoint catalog,
  request/response contracts, errors, and lifecycle semantics.
- [MCP Server](mcp-server.md): transport, authentication, tool catalog,
  orchestration workflow, and REST mapping.
- [Cross-Layer Contracts](cross-layer-contracts.md): compatibility rules,
  known drift, security requirements, and implementation acceptance criteria.
- [Contract Remediation Plan](contract-remediation-plan.md): prioritized decisions,
  implementation steps, compatibility handling, and acceptance tests for the
  identified discrepancies.

## Workspace and session component pages

- [Workspace](PAGE_workspace.md)
- [Workspace Sessions](PAGE_workspace_sessions.md)
- [Workspace Members](PAGE_workspace_members.md)
- [Workspace Settings](PAGE_workspace_settings.md)
- [Session Overview](PAGE_session_overview.md)
- [Session Settings](PAGE_session_settings.md)
- [Session Repositories](PAGE_session_repositories.md)
- [Session Schedules](PAGE_session_schedules.md)
- [Session Logging](PAGE_session_logging.md)
- [Session History](PAGE_session_history.md)
- [Session Terminal](PAGE_session_terminal.md)
- [Session Chat](PAGE_session_chat.md)
- [Session Prompt](PAGE_session_prompt.md)
- [Session Patch](PAGE_session_patch.md)
- [Session Network Rules](PAGE_session_network_rules.md)

## Scope and authority

These documents describe the implementation in this repository. The source of
truth for a field or endpoint is the implementation under `swarmer/models/`,
`swarmer/api/v1/`, `swarmer/api/schemas.py`, and
`mcp-server/agent_swarm_mcp_server/`. Where the implementation has legacy
compatibility fields or known inconsistencies, they are called out explicitly
instead of being presented as a clean redesign.

## Architectural boundary

Swarmer is the control plane and state store. OpenShell owns agent sandboxes,
workspace volumes, provider injection, and exposed services. Swarmer must not
create per-session pods, PVCs, Services, Routes, or credential Secrets.
