# MCP Server Strategy — Proposed Architecture

**Status:** Proposal / design document. Nothing described here is implemented yet.
**Scope:** How Swarmer should support many MCP servers (most of which are Python
packages living in their own git repositories), with both static API-key
credentials and Google OAuth2 (single Client ID, incremental scopes).

This document intentionally does not modify `docs/ARCHITECTURE.md`. That file
contains several stale references to an OAuth 2.1/PKCE flow that was removed
(see §2 below) — reconciling those references is a separate documentation
cleanup task, out of scope here.

---

## 1. Purpose & Scope

Today Swarmer supports exactly one MCP server end to end — **Jira**, using a
static personal API token. Adding a second MCP server today requires manual,
coordinated edits across at least six files (catalog, model, migration,
network policy, OpenCode config generator, UI template) plus a base-image
rebuild, and the current config generator has a latent bug that would make a
second enabled server collide with Jira's hardcoded stanza.

This document proposes a generalized architecture so that:

- Adding a new **static API key** MCP server requires a DB catalog entry, not
  a code change.
- Adding a new **Google OAuth2** MCP server reuses a single, Swarmer-wide
  Google OAuth Client ID ("one ID does it all") with incremental
  per-user-per-workspace consent, rather than a bespoke OAuth integration per
  server.
- The `opencode.json` MCP stanza and sandbox network policy are generated
  generically from catalog data instead of hardcoded per-tool branches.
- MCP server binaries can be either baked into the base agent image (curated,
  fast, security-reviewed) or installed dynamically at session launch
  (long-tail, opt-in), without requiring a new architecture for each mode.

---

## 2. Current State Summary

(File:line references are from `agent-swarm` as of this writing.)

- **Catalog** — `swarmer/mcp_catalog.py:7-22` is a hardcoded Python `list` of
  dicts with a single entry (`atlassian-jira`). `get_catalog_entry()`
  (`mcp_catalog.py:25-29`) is a linear scan. No versioning field, no
  auth-type discriminator, no credential schema.
- **Model** — `swarmer/models/mcp_server.py:34-43` bolts Jira-specific
  columns (`jira_server_url`, `jira_access_token_enc`, `jira_email`) directly
  onto the shared `McpServer` table rather than using a generic credential
  schema. `auth_status`/`is_authenticated` (`mcp_server.py:70-96`) are Jira-
  token-shaped logic.
- **CRUD/Router** — `swarmer/routers/mcp_servers.py` and
  `swarmer/api/v1/mcp_servers.py` implement Jira-only credential save/probe
  logic (`_probe_jira_token`, `McpServerSaveConfig`). **There is no OAuth flow
  anywhere in the current code** — it was explicitly removed in commit
  `def355d` ("replace OAuth with Jira API token auth"). `docs/ARCHITECTURE.md`
  still describes the removed OAuth 2.1/PKCE + dynamic client registration
  design in several places (lines 37, 57, 63, 72, 95, 233, 235) — stale.
- **OpenCode config generation** — `swarmer/agent_tools/opencode.py:57-71`
  iterates `mcp_servers` generically but the loop body ignores everything
  about each server except its `slug`, always emitting the Jira
  command/environment mapping. **A second enabled MCP server today would
  produce a duplicate, incorrect stanza** — a real bug, not just a
  documentation gap.
- **Network policy** — `swarmer/openshell_policy.py:71-107` hardcodes
  `_JIRA_MCP_BLOCK` (endpoints + binaries); `build_session_network_policies()`
  (`openshell_policy.py:392-475`) has one `if "jira" in slug` branch. Binary
  allowlisting must enumerate every possible resolved `python3.x` interpreter
  path because OPA resolves canonical binary paths through symlinks
  (`docs/ARCHITECTURE.md:397-403`) — a manual, iterative "sub-bump loop" is
  required per server, per base image.
- **Packaging** — `agent-containers/containerfiles/Containerfile.agents:137-145`
  pip-installs a pinned wheel (`ARG JIRA_MCP_VERSION`) downloaded from
  `stolostron/jira-mcp-server`'s GitHub Releases into the shared image
  interpreter, at container-build time. Nothing installs MCP servers at
  runtime today; nothing keeps the Containerfile's pinned version and the
  (nonexistent) catalog version field in sync.
- **Underused platform capability** — the OpenShell Gateway already exposes a
  generic `ProviderProfile` concept (`swarmer/openshell_client.py:756-796`,
  `openshell_proto/openshell_pb2.py`) supporting named credentials, a
  `refresh` strategy enum including `OAUTH2_REFRESH_TOKEN` and
  `GOOGLE_SERVICE_ACCOUNT_JWT`, and a `category` taxonomy. This is already
  used — successfully — for Vertex AI ADC-based OAuth2 refresh
  (`openshell_client.py:226-273,319-373`) and for a not-yet-wired-up `"jira"`
  profile registered at startup (`swarmer/main.py:47-60`). **This is the
  mechanism this proposal reuses for Google OAuth2 MCP credentials** — the
  Gateway already knows how to hold a refresh token and mint/refresh access
  tokens server-side; Swarmer does not need to implement its own refresh
  loop.

---

## 3. Design Goals

1. New **static API key** MCP servers are added via a DB catalog entry, not a
   code change + redeploy.
2. New **Google OAuth2** MCP servers reuse one Swarmer-wide Google OAuth
   Client ID; users grant scopes incrementally, per workspace, without a
   bespoke OAuth integration per server.
3. Enabling more than one MCP server in a session produces a correct,
   non-colliding `opencode.json` MCP stanza.
4. Network policy for a new MCP server comes from catalog data, not a new
   Python constant + `if` branch.
5. MCP server binaries can be curated/pre-baked (common case) or
   dynamically installed (long tail), without two divergent architectures.

---

## 4. Proposed Architecture

### 4.1 `McpCatalogEntry` — DB-backed catalog

Replaces the hardcoded list in `mcp_catalog.py`. A new SQLAlchemy model,
managed by admins via the existing MCP servers UI/API (no external registry
repo, no CI pipeline — per current scope decision, this stays inside
Swarmer). An admin adds a new entry by pasting in the MCP server repo's own
`mcp.json` manifest contract (a convention each MCP server repo should
publish for documentation/portability, even though nothing auto-syncs it).

Proposed fields:

| Field | Type | Notes |
|---|---|---|
| `slug` | str, unique | Matches the MCP server's own identifier |
| `display_name` | str | Shown in UI |
| `description` | str | Shown in UI |
| `version` | str | Informational; should track the installed wheel version |
| `command` | JSON list[str] | e.g. `["jira-mcp-server"]` |
| `auth_type` | enum | `static_api_key` \| `oauth2_google` |
| `credential_fields` | JSON | `[{name, secret, label}, ...]` — drives both the DB validation and the UI form |
| `env_mapping` | JSON | Maps credential field names to the `opencode.json` `environment` block, e.g. `{"JIRA_ACCESS_TOKEN": "{env:JIRA_ACCESS_TOKEN}"}` |
| `oauth_scopes` | JSON list[str] | Only for `oauth2_google` entries |
| `network_endpoints` | JSON | `[{host, port?}, ...]` |
| `network_binaries` | JSON | `[path, ...]` |
| `install_mode` | enum | `baked` \| `dynamic` |
| `wheel_source` | str | GitHub Releases URL; used only by the `dynamic` install path |

**Version-drift mitigation:** since there is no external registry keeping the
catalog's `version` field and the Containerfile's pinned wheel version in
sync automatically, add an optional session-launch health check (`<command>
--version` executed in the sandbox) that logs/flags a mismatch against the
catalog's `version` field, rather than relying on manual discipline alone.

### 4.2 Generalized `McpServer` credentials

`McpServer` drops the Jira-specific columns
(`jira_server_url`/`jira_access_token_enc`/`jira_email`) in favor of a single
`credentials_json_enc` column (Fernet-encrypted JSON blob), validated against
the linked `McpCatalogEntry.credential_fields` schema at save time. This
follows the existing `_enc` suffix + `@property` encrypt/decrypt convention
used throughout the codebase (`crypto.py`, `github_pat.py`).

### 4.3 Google OAuth2 — "one Client ID does it all"

- **`GoogleOAuthClient`** — a single, Swarmer-wide (not per-workspace) model
  holding one Google OAuth Client ID/Secret, Fernet-encrypted, configured
  once by an administrator (mirrors the existing `OpencodeSecret` pattern for
  storage).
- **`OAuthGrant`** — one row per `(workspace_id, user_id, provider)`. Stores
  the Fernet-encrypted refresh token and the list of currently-granted
  scopes. Scoped per user *and* per workspace (most granular option) so it's
  always unambiguous whose Google identity and permissions are in effect in
  a given workspace.
- **Flow:** Swarmer hosts its own Authorization Code + PKCE flow using the
  single `GoogleOAuthClient`. When a user enables a Google-backed MCP server
  in a workspace and no `OAuthGrant` exists yet (or the existing grant lacks
  a required scope), Swarmer redirects to Google's consent screen requesting
  only the **incremental** scope delta (`include_granted_scopes=true`),
  reusing the same Client ID. Google merges the new scopes into the same
  underlying grant for that user without requiring them to re-consent to
  previously-granted scopes.
- **Token refresh:** the resulting refresh token is registered with the
  OpenShell Gateway via `ConfigureProviderRefresh` using the
  `OAUTH2_REFRESH_TOKEN` strategy — the exact mechanism already
  battle-tested for Vertex AI ADC (`openshell_client.py:260-266`). The
  Gateway mints and refreshes short-lived access tokens server-side and
  injects them into the sandbox; **Swarmer never implements its own
  token-refresh loop for MCP credentials.**

### 4.4 Auth Strategy pattern

Mirrors the existing `agent_tools/` Strategy pattern
(`swarmer/agent_tools/__init__.py`, `registry.py`). A new
`swarmer/mcp_auth/` package with an `McpAuthStrategy` ABC and two initial
implementations:

- `StaticApiKeyAuthStrategy` — renders a generic form from
  `credential_fields`, validates/stores the `credentials_json_enc` blob,
  implements a generic HTTP health probe (SSRF-guarded, following the
  existing `_is_safe_url()` pattern in `routers/mcp_servers.py:201-216`).
- `GoogleOAuth2AuthStrategy` — renders a "Connect with Google" button,
  drives the incremental-consent redirect/callback, and calls
  `ConfigureProviderRefresh` on success.

### 4.5 Data-driven `opencode.json` generation

Fix `agent_tools/opencode.py:57-71` so the per-server `command`/`environment`
stanza is built from `McpCatalogEntry.command` / `env_mapping` instead of
hardcoded Jira literals. This is a small, contained change to an already
correctly-structured loop, and it resolves the multi-server collision bug
identified in §2.

### 4.6 Data-driven network policy

Replace the `if "jira" in slug: network_policies_dict["jira_mcp"] =
_JIRA_MCP_BLOCK` pattern in `openshell_policy.py:426-427` with a generic loop
over enabled `McpServer` rows, pulling `network_endpoints`/`network_binaries`
from the linked `McpCatalogEntry`. This eliminates the need for a new Python
constant + `if` branch per server. (Scope note: per current decision, this
proposal keeps policy generation catalog-driven only for v1 — it does not
attempt to have the OpenShell Gateway auto-derive policy from an attached
`ProviderProfile`'s `endpoints`/`binaries` fields, even though those fields
exist on the proto; see §8.)

### 4.7 Hybrid packaging: pre-baked + dynamic install

- **Pre-baked (`install_mode=baked`)** — a small script reads all
  `baked` catalog rows and generates the corresponding `RUN pip install`
  blocks for `agent-containers/containerfiles/Containerfile.agents`,
  replacing today's hand-edited `ARG`/`RUN` pairs. Intended for
  common/security-reviewed servers (e.g. Jira).
- **Dynamic (`install_mode=dynamic`)** — installed into the sandbox at
  session-launch time via `exec_command("pip install <wheel_source>")`
  before the agent starts. Gated behind an explicit per-workspace opt-in
  setting and a generic "package installer" network policy block (precedent:
  the existing `_PYTHON_DEVELOPMENT_BLOCK` in `openshell_policy.py:56-69`).
  Trades base-image bloat and long-tail server support for session-launch
  latency and a larger install-time attack surface — this tradeoff should
  remain visible to the workspace admin, not silently defaulted on.

### 4.8 UI changes

Replace the hardcoded Jira credential form in
`templates/mcp_servers/list.html:47-151` with a schema-driven form that
loops over `McpCatalogEntry.credential_fields` (for `static_api_key`
entries) or renders a "Connect with Google" button (for `oauth2_google`
entries). The existing catalog-grid "Available Servers" section
(`list.html:160-166`) is already generic and needs no changes.

---

## 5. Data Model Changes (sketch)

New tables:
- `mcp_catalog_entries` — see §4.1 field list.
- `google_oauth_clients` — one row, Swarmer-wide (or possibly per-instance
  singleton; no per-workspace scoping needed since it's "one ID").
- `oauth_grants` — `(workspace_id, user_id, provider)` unique constraint,
  `scopes` JSON list, `refresh_token_enc`.

Changed tables:
- `mcp_servers` — drop `jira_server_url`, `jira_access_token_enc`,
  `jira_email`; add `catalog_entry_id` (FK), `credentials_json_enc`.

All new encrypted columns follow the `_enc` suffix + `@property`
encrypt/decrypt convention. All new tables require `ALTER TABLE`/`CREATE
TABLE IF NOT EXISTS` entries in `database.py:migrate_db()` per existing
convention.

---

## 6. Flows

**Adding a static-API-key MCP server (admin flow)**
1. Admin pastes the MCP server's `mcp.json` manifest into a new "Add Catalog
   Entry" form → creates an `McpCatalogEntry` row.
2. Workspace member clicks "Add to workspace" on the catalog grid → creates
   an `McpServer` row.
3. Workspace member fills in the schema-driven credential form → validated
   and stored as `credentials_json_enc`.
4. Session launch: `openshell_client` registers/updates a `ProviderProfile`
   or injects `credentials_json_enc` fields as sandbox env vars per
   `env_mapping`; `openshell_policy` adds the catalog's
   `network_endpoints`/`network_binaries`; `opencode.py` writes the MCP
   stanza using `command`/`env_mapping`.

**Adding a Google-OAuth MCP server**
1. Admin adds the catalog entry with `auth_type=oauth2_google` and
   `oauth_scopes`.
2. Workspace member clicks "Connect with Google" → Swarmer computes the
   scope delta against any existing `OAuthGrant` for
   `(workspace_id, user_id, "google")` → redirects to Google consent with
   `include_granted_scopes=true`.
3. Callback exchanges the code for a refresh token → upserts `OAuthGrant`.
4. Swarmer calls `ConfigureProviderRefresh` (`OAUTH2_REFRESH_TOKEN`
   strategy) so the Gateway can mint/refresh access tokens for this
   sandbox's sessions going forward.
5. Session launch: Gateway injects a live access token per `env_mapping`;
   no Swarmer-side refresh logic required.

**Session launch (both types)**
1. `_do_launch()` collects enabled `McpServer` rows for the workspace/user.
2. `openshell_policy.build_session_network_policies()` assembles policy
   generically from each server's catalog entry.
3. `openshell_client` attaches/creates providers and injects credentials
   (static values or Gateway-refreshed OAuth tokens).
4. `agent_tools/opencode.py:build_config_data()` writes the `mcp` stanza
   generically from each server's catalog `command`/`env_mapping`.

---

## 7. Phased Rollout Plan

- **Phase 1** — Generalize `McpCatalogEntry` + `McpServer` model; migrate
  Jira to the new schema as the first (and initially only) catalog entry;
  fix the `opencode.json` multi-server bug. No OAuth yet.
- **Phase 2** — Data-driven network policy (§4.6), replacing the
  Jira-specific `if` branch.
- **Phase 3** — Google OAuth2 "one Client ID" flow: `GoogleOAuthClient`,
  `OAuthGrant`, incremental consent, Gateway `ConfigureProviderRefresh`
  wiring (§4.3).
- **Phase 4** — Hybrid packaging: Containerfile-generation script for
  `baked` entries; opt-in dynamic install path for `dynamic` entries (§4.7).
- **Phase 5** — Schema-driven UI (§4.8).

Each phase should ship independently and keep Jira working throughout —
Phase 1 in particular should be validated by porting Jira itself onto the
new generic schema before any second MCP server is added.

---

## 8. Open Risks / Follow-ups

- `docs/ARCHITECTURE.md` contains multiple stale references to the removed
  OAuth 2.1/PKCE flow (lines 37, 57, 63, 72, 95, 233, 235 as of this
  writing). Recommend a documentation-only cleanup pass, tracked
  separately from this proposal.
- Whether the OpenShell Gateway auto-enforces a `ProviderProfile`'s
  `endpoints`/`binaries` fields when that profile is attached to a sandbox
  is **unconfirmed**. If it does, network policy for new MCP servers could
  eventually require zero Swarmer-side policy code (just importing the
  profile with its manifest-declared endpoints/binaries). This is
  explicitly out of scope for the v1 (catalog-driven) design in §4.6 and
  should be investigated as a fast-follow once Phase 2 ships.
  This includes the OPA
  canonical-binary-path gotcha (`ARCHITECTURE.md:397-403`); compiling MCP
  servers to standalone binaries (e.g. via PyInstaller) at build time would
  remove the need to enumerate every possible interpreter path, but is a
  meaningfully larger lift than this proposal's Phase 4 and is not included
  here.
- Security review is needed for the dynamic (`install_mode=dynamic`)
  install path before enabling it by default for any workspace — installing
  arbitrary wheels into a live sandbox at launch time is a materially larger
  attack surface than today's build-time-only model, even within an
  isolated OpenShell sandbox.
- No external MCP registry repo/CI is proposed in this version — catalog
  curation is entirely manual (admin paste-in). If the number of MCP servers
  grows large, revisit whether a lightweight external index becomes
  worthwhile to reduce manual version-drift risk (§4.1).
