# Agent Swarm Use Cases

Agent Swarm provides interactive development, autonomous SDLC automation, and
secure OpenShell-backed execution for coding agents.

## Personal Development

### Browser TUI

Developers can access OpenCode agents, interactive shells, and Git from
ChromeOS or another browser without installing a local development environment.
Swarmer connects the browser terminal to an OpenShell PTY over WebSockets.

### Mobile-Friendly Chat

Server-mode sessions expose the OpenCode web interface through Swarmer's
authenticated chat proxy. Developers can use a responsive, multi-session
interface from phones, tablets, or desktop browsers.

### Git and Jira Workflow

Sessions can attach repositories and work in isolated branches. Agents can use
GitHub and Jira through configured MCP integrations to inspect work items,
update issues, add comments, transition status, and connect code changes to
project work.

### LSP-Enabled Development

Swarmer writes a valid OpenCode configuration into each sandbox and can enable
language servers such as `gopls` and `pyright-langserver`. This gives agents
language-aware diagnostics, navigation, and reference discovery rather than
relying only on text search.

## Models and Runtime Images

- Google Gemini can be used through Google AI Studio credentials.
- Anthropic models can be used through Vertex AI.
- OpenAI models can be selected through OpenAI provider credentials.
- OpenCode and shell sessions can use custom container images.
- Python-capable images support repository scripts, audits, dependency tools,
  and automation.
- Kubernetes image pull secrets allow private agent images to be used without
  embedding registry credentials in application configuration.

OpenCode provider presets can define separate plan, build, and small models.
The shell agent tool runs deterministic commands without invoking an LLM,
which is useful for scheduled scripts and token-free automation.

## GitHub App Authentication

Workspace GitHub Apps provide repository access without relying on a personal
long-lived token. Private keys are encrypted in the database, short-lived
installation access tokens are minted when needed, and long-running TUI and
server sessions refresh their tokens through the OpenShell provider. Refresh
processing is restored for surviving sessions after a Swarmer restart.

## OpenShell Backend

OpenShell is the session runtime. It provides sandbox lifecycle management,
filesystem and process isolation, network policy enforcement, services,
interactive PTYs, and provider-backed credential delivery.

### Credential and Token Proxy

AI, GitHub, and Jira credentials are registered with the OpenShell Gateway
Provider API. The gateway supplies credentials to the sandbox at runtime,
avoiding direct injection of raw long-lived secrets into session configuration
or repository files.

### Network Policy Learning

Sessions start with network rules computed from their repositories, prompt
sources, MCP servers, agent tool, and model provider. When a process attempts
an unapproved connection, the OpenShell supervisor can produce a draft policy
chunk.

Users can review draft chunks in Swarmer and approve appropriate rules. Approved
rules become session custom policies, can be applied to a running sandbox where
supported, and are persisted for later launches. Users can inspect and revoke
custom rules. This allows a restrictive default policy while retaining useful
knowledge between otherwise stateless runs.

## Session Execution Modes

- **Prompt:** Run a one-shot task, preserve the agent output and raw log, and
  clean up the sandbox after successful completion.
- **TUI:** Keep an interactive terminal session available through the browser.
- **Server:** Keep an OpenCode server running and expose its web interface
  through the chat proxy.
- **Shell:** Run deterministic commands without an AI model.

Prompt mode is useful for stateless jobs because each run starts from the
configured inputs, captures its results and policy observations, and removes
the sandbox when complete.

## Autonomous SDLC Workflows

- Weekday cron schedules run CVE and security audits.
- Monday cron schedules check for dependency updates.
- Multi-release prompts audit or update multiple release branches in one run.
- `new_pr_or_commit` event schedules launch automated team PR reviews.
- `review_comments` event schedules launch repair agents for bot-authored PRs.
- Hourly cron schedules perform PR hygiene during configured working hours.
- `review_approved` event schedules perform post-approval hygiene for bot
  PRs.

Each `SessionSchedule` has exactly one trigger type: `cron` or `event`. A
session can have multiple independent schedule rows, but one schedule cannot
combine both trigger types. Event schedules can also specify author scope,
event context, and a quiet-period delay.

### Maintainer Executive Digest

For a popular CNCF project with 1,000+ stars and fully public repositories,
maintainers can set up a daily or weekly cronjob that reviews the entire org
across all repos without requiring any GitHub credentials. The workflow can:

- Collect newly opened issues and recently created or updated pull requests.
- Triage each item in depth, tracking the latest discussion threads,
  reviewer feedback, and maintainers' notes.
- Score issues and PRs by urgency, blast radius, confidence, and maintenance
  risk so the busiest signals surface first.
- Compare the current state against `coderabbitai` suggested fixes and
  `dosubot` triage guidance to propose next actions.
- Export suggested fixes as downloadable `.patch` files so follow-up work can
  be applied later without granting direct write access.
- Pace GitHub API calls intelligently to avoid rate-limit spikes and retry
  storms during large-org sweeps.

The result is a single executive summary after each scheduled run that gives a maintainer
or project lead a fast read on org-wide health, plus a drill-down path for
opening the full context of any issue, PR, discussion, or recommended fix
when deeper investigation is needed. The application records each terminal
run's output and trigger metadata in run history. Scheduled runs can repeat as
new activity appears, but they do not inherit prior prompt context.

## HyperShift and CNV (Workload Management)

The HyperShift and CNV integration teams use Agent Swarm for scheduled operational
digests and Jira CVE handoff. Prompts live in a git-backed workspace prompt library
([swarm-prompt](https://github.com/yiraeChristineKim/swarm-prompt)); Swarmer runs
them as **prompt-mode** sessions with optional **cron** schedules.

### Morning PR digest (HyperShift / MTV repos)

Weekday cron (for example `0 13 * * 1-5` when the cluster clock is UTC, ≈ 09:00
America/New_York) scans open pull requests in:

- [stolostron/mtv-integrations](https://github.com/stolostron/mtv-integrations)
- [stolostron/hypershift-addon-operator](https://github.com/stolostron/hypershift-addon-operator)

The agent classifies each open PR (needs review, changes requested, approved,
draft, CI blocked) and posts a Slack mrkdwn summary to
`#acm-hypershift-mtv-notification` using the workspace `SLACK_WEBHOOK_URL`.
See [docs/SLACK_NOTIFICATIONS.md](SLACK_NOTIFICATIONS.md) for webhook setup.

| Trigger | Session mode | Prompt source |
|---------|--------------|---------------|
| `cron` | Prompt | `morning-pr-digest-hypershift-mtv.md` |

**Inputs:** `gh` / GitHub App auth in the sandbox, `SLACK_WEBHOOK_URL` on the
workspace.

**Outputs:** Slack digest; no repository writes.

### CVE ACM → OCPBUGS (MCE pscomponents)

On demand or on a schedule, a prompt-mode session moves open ACM `Vulnerability`
issues (assigned to the operator) into **OCPBUGS** using a **real Jira project
Move** (`ACM-XXXXX` becomes `OCPBUGS-NNNNN`), then sets component, OCP Affects /
Target versions, MCE version comments, and component auto-assign. A Slack
summary lists what was moved.

| Workflow | ACM pscomponent | OCPBUGS component |
|----------|-----------------|-------------------|
| A | `multicluster-engine/cluster-api-provider-kubevirt-rhel9` | HyperShift / OCP Virtualization |
| B | `multicluster-engine/cluster-api-provider-azure-rhel9` | HyperShift / ARO |
| C | `multicluster-engine/hypershift-rhel9-operator` | HyperShift |
| C | `multicluster-engine/hypershift-cli-rhel9` | HyperShift |

**Out of scope:** `multicluster-engine/hypershift-addon-rhel9-operator` (do not
move).

MCE → OCP mapping used in the prompt (examples: 2.10 → 4.20, 2.11 → 4.21,
2.17 → 4.22). Full table is in `cve-acm-to-ocpbugs.md`.

| Trigger | Session mode | Prompt source |
|---------|--------------|---------------|
| `cron` or manual | Prompt | `cve-acm-to-ocpbugs.md` |

**Inputs:** Jira MCP (search, update, comment), Jira Bulk Move REST API when MCP
has no move tool, `SLACK_WEBHOOK_URL`.

**Outputs:** Moved OCPBUGS issues, ACM comments/links, Slack handoff summary.

Documented for parent spike [ACM-46087](https://redhat.atlassian.net/browse/ACM-46087);
sub-task [ACM-46099](https://redhat.atlassian.net/browse/ACM-46099).

## Inputs and Outputs

Typical inputs include repositories and branches, prompt-library entries,
session mode, model/provider selection, MCP servers, GitHub credentials, Jira
credentials, custom images, pull secrets, and approved network policies.

Typical outputs include:

- Agent conversation output, raw logs, and run history
- Commits, branches, pull requests, review comments, labels, and approvals
- Jira comments, field updates, transitions, and logged work
- Sandbox service URLs for server-mode sessions
- Draft and approved network policy rules
- Security audit and dependency update results
