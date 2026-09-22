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
  clean up the sandbox after completion.
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
