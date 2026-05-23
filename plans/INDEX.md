# Plans Index

Parent epic: [ACM-32892](https://redhat.atlassian.net/browse/ACM-32892) — Implement an Agentic SDLC platform, similar to Ambient and KubeOpenCode

## Sessions

---
- date: "2026-04-29"
  title: "Switch swarmer base image from python:3.12-slim to UBI9"
  jira: "ACM-33416"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33416"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/33"
  summary: "Switched to ubi9/python-312-minimal:latest (62.5 MB, non-root uid 1001); dropped apt-get block; added USER 0/1001 sandwich for group-0-writable PVC mount points"
# ──────────────────────────────────────────────────────────
- date: "2026-04-29"
  title: "Reuse agent image for git-init init container"
  jira: "ACM-33416"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33416"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/34"
  summary: "Replaced alpine/git:latest (Docker Hub) with tool.get_image() so the git-init init container reuses the same agent image (OpenCode has git + gh; Crush has git); eliminates Docker Hub dependency"
# ──────────────────────────────────────────────────────────
- date: "2026-05-01"
  title: "Move Launch/Stop controls from Configuration card to status bar"
  jira: "ACM-33539"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33539"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/39"
  summary: "Relocated Launch/Stop controls to status bar; moved Prompt above tabs with collapse/expand toggle; removed Last Output from Details tab; auto-focus Output/Terminal tab after Launch; auto-clear output on launch; removed Clean Up button in prompt mode; re-enabled Launch after completion in prompt mode"
# ──────────────────────────────────────────────────────────
- date: "2026-05-01"
  title: "Sessions detail: Git Repos + Schedule to Details tab, fix Prompt toggle and Output marker"
  jira: "ACM-33548"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33548"
  status: "Closed"
  pr: "https://github.com/stolostron/agent-swarm/pull/40"
  summary: "Failed-state relaunch; removed Clean Up concept; prompt textarea visible in all modes (AGENTS.md injection for TUI/server); TUI 4s reload delay + window.load auto-connect fix; breadcrumb layout fixed across all 11 templates (correct PF v6 divider placement, flex layout, leading slash, 1.125rem font)"
# ──────────────────────────────────────────────────────────
- date: "2026-05-01"
  title: "Bridge OSC 52 clipboard sequences from pod to browser xterm.js terminal"
  jira: "ACM-33551"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33551"
  status: "Closed"
  pr: "https://github.com/stolostron/agent-swarm/pull/41"
  summary: "Add OSC 52 handler to xterm.js so TUI clipboard copies in the pod reach the user's browser clipboard"
# ──────────────────────────────────────────────────────────
- date: "2026-05-04"
  title: "Investigate and fix PVC lifecycle between session runs: persistence and cleanup"
  jira: "ACM-33566"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33566"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/44"
  summary: "Fixed orphaned PVCs on prompt-mode completion: _auto_cleanup_pod now deletes the PVC when persist is disabled, matching the existing Stop button behaviour"

# ──────────────────────────────────────────────────────────
- date: "2026-05-04"
  title: "Reduce default workspace PVC size from 10Gi to 5Gi"
  jira: "ACM-33570"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33570"
  status: "Closed"
  pr: "https://github.com/stolostron/agent-swarm/pull/45"
  summary: "Reduced default workspace PVC size from 10Gi to 5Gi by updating the storage default in ensure_session_pvc() in swarmer/k8s_session.py."

# ──────────────────────────────────────────────────────────
- date: "2026-05-05"
  title: "Add Crush support for small and large models"
  jira: "ACM-33630"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33630"
  status: "Closed"
  pr: "https://github.com/stolostron/agent-swarm/pull/50"
  summary: "Added `_derive_small_model()` to crush.py: Opus→Sonnet, Sonnet→Haiku (vertexai/anthropic), Gemini Pro→Flash; CrushStrategy now injects models.small into the Crush config when derivable"

# ──────────────────────────────────────────────────────────
- date: "2026-05-06"
  title: "Switch Jira MCP from Atlassian OAuth to token-based binary MCP (mcp-atlassian)"
  jira: "ACM-33664"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33664"
  status: "Closed"
  pr: "https://github.com/stolostron/agent-swarm/pull/52"
  summary: "Localhost OAuth redirect hack: Connect opens in new tab with localhost:18080 callback; MCP card shows paste input so user can submit the failed callback URL to complete token exchange via new /oauth-complete route"

# ──────────────────────────────────────────────────────────
- date: "2026-05-06"
  title: "Sync crush image tag into agent-swarm from agent-containers build"
  jira: "ACM-33677"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33677"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/53"
  summary: "Extended sync-images to also write AGENT_IMAGE_CRUSH=$(REGISTRY)/crush:$(TAG) into .env from agent-containers/.push-defaults, replacing the hardcoded ghcr.io/gurnben/crush-container:latest"

# ──────────────────────────────────────────────────────────
- date: "2026-05-06"
  title: "Add Jira API token auth to replace OAuth flow for jira-mcp-server binary"
  jira: "ACM-33691"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33691"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/54"
  summary: "Replaced entire OAuth flow with API token form (server URL, token, email); added Jira API probe validation + 60s auto-refresh; expired tokens greyed out in session views; cleaned up all OAuth model columns, properties, and legacy HTTP branches"
# ──────────────────────────────────────────────────────────
- date: "2026-05-07"
  title: "Upgrade Opus model from claude-opus-4-6 to claude-opus-4-7 in Crush and OpenCode tools"
  jira: "ACM-33695"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33695"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/55"
  summary: "Replace all claude-opus-4-6 model IDs with claude-opus-4-7 in swarmer/agent_tools/crush.py and opencode.py"
# ──────────────────────────────────────────────────────────
- date: "2026-05-07"
  title: "Clean up image reachability check logs for public images"
  jira: "ACM-33706"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33706"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/57"
  summary: "Downgraded missing-pull-secret warnings to debug for public images; added Opus 4.6 back to model lists so existing sessions match"

# ──────────────────────────────────────────────────────────
- date: "2026-05-07"
  title: "Move .push-defaults to agent-swarm as source of truth for REGISTRY and IMAGE_TAG"
  jira: "ACM-33878"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33878"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/61"
  summary: "Moved REGISTRY + IMAGE_TAG source of truth to agent-swarm/.push-defaults (checked in); updated agent-containers Makefile and all 6 scripts to resolve from ../agent-swarm first with local fallback for machine-specific fields"
  
- date: "2026-05-07"
  title: "Improve secrets security: session-scoped K8s secrets, per-user credential isolation, and cleanup lifecycle"
  jira: "ACM-33880"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33880"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/59"
  summary: "Session-scoped K8s secrets created at launch and cleaned up on stop; per-user credential isolation with shared flag; purge/audit plan for orphaned secrets"

# ──────────────────────────────────────────────────────────
- date: "2026-05-08"
  title: "Investigate VertexAI model listing by credential and remove unavailable Opus 4.7 choice"
  jira: "ACM-33890"
  jira_url: "https://redhat.atlassian.net/browse/ACM-33890"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/62"
  summary: "VertexAI model listing API does not filter by credential; removed claude-opus-4-7 from all model choice lists in crush.py and opencode.py, promoted opus-4-6 to most capable"

# ──────────────────────────────────────────────────────────
- date: "2026-05-15"
  title: "Increase Agent pod memory limit from 4Gi to 8Gi"
  jira: "ACM-34126"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34126"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/69"
  summary: "Raised agent container memory limit from 4Gi to 8Gi in k8s_session.py to support larger codebases and extended context windows"

# ──────────────────────────────────────────────────────────
- date: "2026-05-15"
  title: "Enable LSPs in dynamic OpenCode and Crush configs"
  jira: "ACM-34104"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34104"
  status: "Done"
  pr: ~
  summary: "Added lsp: true to OpenCode and auto_lsp + explicit gopls/pyright entries to Crush in both build_config_data() and build_mcp_config_cmd(), fixing runtime config overwriting static LSP settings"

# ──────────────────────────────────────────────────────────
- date: "2026-05-15"
  title: "Workspace prompt library: named prompts, URL-based prompt sources, and per-session prompt picker"
  jira: "ACM-34123"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34123"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/72"
   summary: "Implemented a workspace-level prompt library with git-backed folders, recursive .md file caching, and an HTMX-powered per-session prompt picker with live preview."

# ──────────────────────────────────────────────────────────
- date: "2026-05-20"
  title: "Add REST API server to agent-swarm alongside existing Console"
  jira: "ACM-34254"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34254"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/75"
  summary: "Added 51 REST API endpoints under /api/v1/ with K8s bearer token auth, Pydantic schemas, and 32 unit tests alongside the existing HTMX Console"

# ──────────────────────────────────────────────────────────
- date: "2026-05-20"
  title: "Refactor Console routes to consume REST API instead of direct DB/K8s access"
  jira: "ACM-34269"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34269"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/76"
  summary: "Refactored 6 Console route handlers (workspaces, env_vars, secrets, mcp_servers, prompts, auth) to call /api/v1/ via internal API client with httpx ASGI transport, DotDict template compat, and 27 new unit tests"

# ──────────────────────────────────────────────────────────
- date: "2026-05-20"
  title: "Update Gemini model IDs from gemini-3-flash/pro to gemini-3.5-flash/pro in Crush and OpenCode tools"
  jira: "ACM-34288"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34288"
  status: "Done"
  pr: "https://github.com/stolostron/agent-swarm/pull/77"
  summary: "Updated all Gemini model IDs from gemini-3-flash/pro to gemini-3.5-flash/pro in opencode.py and crush.py, fixed preivew typo and mismatched 2.5 labels"

# ──────────────────────────────────────────────────────────
- date: "2026-05-23"
  title: "Inject dynamic repository context into AGENTS.md and prompt text for all session modes"
  jira: "ACM-34355"
  jira_url: "https://redhat.atlassian.net/browse/ACM-34355"
  pr: "https://github.com/stolostron/agent-swarm/pull/79"
  summary: "Centralize repo context generation in k8s_session.py and inject structured markdown table into AGENTS.md (TUI/server) and prompt text (prompt mode), extending ACM-32956 to all session modes"

## Foundation & Feature Plans

| Plan | Summary | Jira | PR |
|------|---------|------|-----|
| [2026-04-14-01-foundation.md](2026-04-14-01-foundation.md) | Bootstrap FastAPI + auth + K8s base | [ACM-32894](https://redhat.atlassian.net/browse/ACM-32894) | [#1](https://github.com/stolostron/agent-swarm/pull/1) |
| [2026-04-14-02-workspaces.md](2026-04-14-02-workspaces.md) | Workspace CRUD + namespace mgmt | [ACM-32895](https://redhat.atlassian.net/browse/ACM-32895) | [#1](https://github.com/stolostron/agent-swarm/pull/1) |
| [2026-04-14-03-secrets.md](2026-04-14-03-secrets.md) | Per-workspace secrets + K8s sync | [ACM-32896](https://redhat.atlassian.net/browse/ACM-32896) | [#1](https://github.com/stolostron/agent-swarm/pull/1) |
| [2026-04-14-04-sessions.md](2026-04-14-04-sessions.md) | Session lifecycle + TUI xterm.js | [ACM-32897](https://redhat.atlassian.net/browse/ACM-32897) | [#1](https://github.com/stolostron/agent-swarm/pull/1) |
| [2026-04-14-05-pull-secret-tab.md](2026-04-14-05-pull-secret-tab.md) | Pull secret tab + secrets page redesign | [ACM-32898](https://redhat.atlassian.net/browse/ACM-32898) | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-14-06-ui-tui-improvements.md](2026-04-14-06-ui-tui-improvements.md) | Branding, TUI full-width, model default | [ACM-32899](https://redhat.atlassian.net/browse/ACM-32899) | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-14-07-model-id-fixes.md](2026-04-14-07-model-id-fixes.md) | Model ID format + all-mode picker | [ACM-32900](https://redhat.atlassian.net/browse/ACM-32900) | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-15-09-context-repositories-prompt-injection.md](2026-04-15-09-context-repositories-prompt-injection.md) | Inject cloned repos into prompt | [ACM-32956](https://redhat.atlassian.net/browse/ACM-32956) | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-15-08-multi-agent-tool-architecture.md](2026-04-15-08-multi-agent-tool-architecture.md) | AgentToolStrategy + Crush support design | — | [#4](https://github.com/stolostron/agent-swarm/pull/4) |
| [2026-04-15-09-execution-plan.md](2026-04-15-09-execution-plan.md) | 5-phase Crush integration execution plan | — | [#4](https://github.com/stolostron/agent-swarm/pull/4) |
| [2026-04-15-11-language-image-selector.md](2026-04-15-11-language-image-selector.md) | Per-session language/image selector | — | [#4](https://github.com/stolostron/agent-swarm/pull/4) |
| [2026-04-17-14-agent-launch-buttons.md](2026-04-17-14-agent-launch-buttons.md) | Per-tool launch buttons with availability | — | [#4](https://github.com/stolostron/agent-swarm/pull/4) |
| [2026-04-17-15-config-autosave.md](2026-04-17-15-config-autosave.md) | Config auto-save on change | — | [#7](https://github.com/stolostron/agent-swarm/pull/7) |
| [2026-04-20-k8s-token-auth.md](2026-04-20-k8s-token-auth.md) | K8s bearer token auth + OpenShift OAuth | — | [#12](https://github.com/stolostron/agent-swarm/pull/12) |
| [2026-04-21-coderabbit-fixes.md](2026-04-21-coderabbit-fixes.md) | CodeRabbit PR #12 security/async fixes | [ACM-33110](https://redhat.atlassian.net/browse/ACM-33110) | [#12](https://github.com/stolostron/agent-swarm/pull/12) |
| [2026-04-22-session-launch-ui-refactor.md](2026-04-22-session-launch-ui-refactor.md) | Single Launch button + image dropdown | [ACM-33184](https://redhat.atlassian.net/browse/ACM-33184) | [#16](https://github.com/stolostron/agent-swarm/pull/16) |
| [2026-04-23-session-prompt-ux.md](2026-04-23-session-prompt-ux.md) | Prompt card, auto pod cleanup, timing | [ACM-33192](https://redhat.atlassian.net/browse/ACM-33192) | [#17](https://github.com/stolostron/agent-swarm/pull/17) |
| [2026-04-23-ansi-color-output.md](2026-04-23-ansi-color-output.md) | ANSI → HTML spans in session output | [ACM-33206](https://redhat.atlassian.net/browse/ACM-33206) | [#21](https://github.com/stolostron/agent-swarm/pull/21) |
| [2026-04-23-openshift-deploy-scc-fixes.md](2026-04-23-openshift-deploy-scc-fixes.md) | OAuthClient fix, SCC RoleBinding | [ACM-33229](https://redhat.atlassian.net/browse/ACM-33229) | [#26](https://github.com/stolostron/agent-swarm/pull/26) |
| [2026-04-23-cve-prompts-repo-design.md](2026-04-23-cve-prompts-repo-design.md) | CVE prompts repo consolidation | — | — (stolostron/cve-prompts) |
| [2026-04-27-sessions-list-launch-refresh.md](2026-04-27-sessions-list-launch-refresh.md) | Launch/Stop buttons, 3s refresh, elapsed time | [ACM-33311](https://redhat.atlassian.net/browse/ACM-33311) | [#29](https://github.com/stolostron/agent-swarm/pull/29) |
| [2026-04-28-tui-patch-export.md](2026-04-28-tui-patch-export.md) | TUI patch export with AI-generated commit messages | [ACM-33411](https://redhat.atlassian.net/browse/ACM-33411) | [#32](https://github.com/stolostron/agent-swarm/pull/32) |
| [2026-04-29-session-cron-scheduling.md](2026-04-29-session-cron-scheduling.md) | Cron scheduling for prompt-mode sessions | [ACM-33440](https://redhat.atlassian.net/browse/ACM-33440) | [#37](https://github.com/stolostron/agent-swarm/pull/37) |

## Bug Fixes

| Plan | Summary | Jira | PR |
|------|---------|------|-----|
| [2026-04-15-08-fix-pat-form-action-url.md](2026-04-15-08-fix-pat-form-action-url.md) | GitHub PAT form 405 URL bug | [ACM-32955](https://redhat.atlassian.net/browse/ACM-32955) | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-15-10-repo-list-local-path-layout.md](2026-04-15-10-repo-list-local-path-layout.md) | Repo list column layout fix | — | [#2](https://github.com/stolostron/agent-swarm/pull/2) |
| [2026-04-15-12-fix-image-reachability-check.md](2026-04-15-12-fix-image-reachability-check.md) | Image reachability always-false fix | — | [#10](https://github.com/stolostron/agent-swarm/pull/10) |
| [2026-04-17-13-fix-repo-add-lazy-load.md](2026-04-17-13-fix-repo-add-lazy-load.md) | repo_add 500: SQLAlchemy async lazy-load | [ACM-33014](https://redhat.atlassian.net/browse/ACM-33014) | [#5](https://github.com/stolostron/agent-swarm/pull/5) / [#7](https://github.com/stolostron/agent-swarm/pull/7) |
| [2026-04-19-16-last-output-panel-fix.md](2026-04-19-16-last-output-panel-fix.md) | Last Output panel height constraint | [ACM-33033](https://redhat.atlassian.net/browse/ACM-33033) | [#9](https://github.com/stolostron/agent-swarm/pull/9) |
| [2026-04-27-k8s-deploy-agent-image-env.md](2026-04-27-k8s-deploy-agent-image-env.md) | k8s-deploy missing AGENT_IMAGE env vars | [ACM-33310](https://redhat.atlassian.net/browse/ACM-33310) | [#27](https://github.com/stolostron/agent-swarm/pull/27) |

## Future / Open

| Summary | Jira | PR |
|---------|------|-----|
| Native Swarmer Bootstrap chat for opencode serve | [ACM-33417](https://redhat.atlassian.net/browse/ACM-33417) | — |

## PRs without a plan file

| PR | Summary | Jira |
|----|---------|------|
| [#3](https://github.com/stolostron/agent-swarm/pull/3) | Non-root session pod fix | — |
| [#6](https://github.com/stolostron/agent-swarm/pull/6) / [#8](https://github.com/stolostron/agent-swarm/pull/8) / [#11](https://github.com/stolostron/agent-swarm/pull/11) / [#18](https://github.com/stolostron/agent-swarm/pull/18) / [#22](https://github.com/stolostron/agent-swarm/pull/22) / [#28](https://github.com/stolostron/agent-swarm/pull/28) | Dependency updates | — |
| [#13](https://github.com/stolostron/agent-swarm/pull/13) | Crush TUI --model error + OpenShift non-root UID | — |
| [#14](https://github.com/stolostron/agent-swarm/pull/14) / [#20](https://github.com/stolostron/agent-swarm/pull/20) | PatternFly migration | PEARCH-43 |
| [#15](https://github.com/stolostron/agent-swarm/pull/15) | Kustomize deployment alternative | — |
| [#19](https://github.com/stolostron/agent-swarm/pull/19) | Strip whitespace from pasted bearer tokens | — |
| [#23](https://github.com/stolostron/agent-swarm/pull/23) / [#24](https://github.com/stolostron/agent-swarm/pull/24) | Start timestamp + duration columns | [ACM-33213](https://redhat.atlassian.net/browse/ACM-33213) / [ACM-33220](https://redhat.atlassian.net/browse/ACM-33220) |
| [#25](https://github.com/stolostron/agent-swarm/pull/25) / [#31](https://github.com/stolostron/agent-swarm/pull/31) | OpenShift SCC + non-root compat | — |
| [#30](https://github.com/stolostron/agent-swarm/pull/30) | INDEX.md update | — |

