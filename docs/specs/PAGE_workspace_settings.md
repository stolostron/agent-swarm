# Workspace Settings Components

## Purpose

Workspace settings group the configuration consumed by sessions: AI provider
credentials, GitHub credentials, MCP servers, environment variables, and prompt
sources. These are separate components because each has its own persistence and
security behavior.

## Sources

- Templates: `swarmer/templates/secrets/*`, `swarmer/templates/mcp_servers/*`,
  `swarmer/templates/env_vars/*`, `swarmer/templates/prompts/*`
- Routers: `swarmer/routers/secrets.py`, `mcp_servers.py`, `env_vars.py`,
  `prompts.py`
- REST modules: `swarmer/api/v1/secrets.py`, `mcp_servers.py`, `env_vars.py`,
  `prompts.py`
- Contracts: [REST API](rest-api.md), [Data Model](data-model.md),
  [Cross-Layer Contracts](cross-layer-contracts.md)

## Methods and tooling

- Jinja2/PatternFly forms and tabbed partials.
- POST/redirect/GET for HTML mutations.
- HTMX partial refreshes where list state changes in place.
- REST CRUD for MCP, prompt, PAT, provider, GitHub App, environment, and pull
  secret resources.
- `crypto.encrypt()`/`crypto.decrypt()` for persisted sensitive values.
- OpenShell provider injection for credentials used by sandboxes.

## Component responsibilities

- AI tokens: save/delete supported provider credentials and show health without
  returning secret material.
- GitHub PAT/App: provide repository discovery and session authentication;
  private keys remain encrypted and never enter a sandbox.
- MCP servers: store endpoint/auth metadata, test health, enable/disable, and
  expose only active configurations to session setup. Server records and their
  credentials are shared with sessions in the workspace, including background runs.
- Environment variables: store workspace-scoped encrypted values and inject
  them through the approved sandbox mechanism.
- Prompts: manage git-backed prompt sources, refresh them, browse repositories
  and folders, and preview selectable prompts.

## API references

- `/api/v1/workspaces/{ws_id}/secrets/*`
- `/api/v1/workspaces/{ws_id}/mcp-servers/*`
- `/api/v1/workspaces/{ws_id}/env-vars/*`
- `/api/v1/workspaces/{ws_id}/prompts/*`

## Security invariants

- Never render or return plaintext tokens, passwords, private keys, or API
  credentials after persistence.
- Validate workspace ownership on every child resource operation.
- A session receives only explicitly selected MCP servers and credentials.
- MCP server credentials are available to workspace sessions and background jobs;
  tokens remain encrypted at rest and are never returned in API responses.
- Gateway configuration replacement must preserve all intended fields and must
  be connection-tested before reporting success.
