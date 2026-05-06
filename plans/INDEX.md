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

