# Slack Notifications Setup

Agent Swarm does not send Slack messages itself. Instead, **agent prompts and workflows**
running inside sessions can post to Slack when they finish (bug triage digests, CVE reports,
PR reminders, and similar). Those workflows read `SLACK_WEBHOOK_URL` from the agent process
environment.

Configure Slack by setting that variable on the **workspace**. Every session launched in that
workspace inherits it automatically.

---

## How it works

1. You store `SLACK_WEBHOOK_URL` as a **workspace environment variable** in Swarmer.
2. At session launch, Swarmer decrypts the value and injects it into the OpenShell sandbox
   agent process (along with any other workspace env vars).
3. When the agent prompt reaches its Slack phase, it calls the webhook (for example via
   `send_to_slack.sh` in [server-foundation-agent](https://github.com/stolostron/server-foundation-agent)
   prompts).

Workspace env vars apply to **all session modes** (prompt, server, and TUI) and to **cron-scheduled**
prompt sessions.

> **Note:** Swarmer has no per-session Slack toggle in the UI. Sessions inherit workspace env vars.
> Use separate workspaces (each with its own webhook) when different sessions should post to
> different channels.

---

## Step 1: Create a Slack Incoming Webhook

1. Open [Slack API — Your Apps](https://api.slack.com/apps) and create or select an app for
   your workspace.
2. Enable **Incoming Webhooks**.
3. Click **Add New Webhook to Workspace** and pick the target channel.
4. Copy the webhook URL. It looks like:
   `https://hooks.slack.com/services/T…/B…/…`

Each webhook posts to **one channel**. Create additional webhooks (or workspaces) for other
channels.

---

## Step 2: Configure the workspace (UI)

1. Log in to Swarmer and open the workspace that runs your scheduled or on-demand sessions.
2. From the workspace sessions page, click **Environment Variables** (or go to
   `/workspaces/{id}/env-vars`).
3. Add:

   | Key | Value |
   |-----|-------|
   | `SLACK_WEBHOOK_URL` | Your Incoming Webhook URL from Step 1 |

4. Click **Save Variable**.

Values are **Fernet-encrypted at rest** in the Swarmer database and masked in the UI. They are
only decrypted when a session launches and the value is passed into the sandbox.

---

## Verifying the setup

### Check env injection

1. Create a **TUI** session in the workspace (keeps the sandbox running).
2. Attach to the terminal and run:
   ```bash
   test -n "$SLACK_WEBHOOK_URL" && echo "SLACK_WEBHOOK_URL is set" || echo "NOT SET"
   ```
3. Do **not** echo the full URL in shared logs — treat it as a secret.

### Send a test message

From the same TUI session (requires `curl` in the agent image):

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST "$SLACK_WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Swarmer Slack test — workspace env var OK"}'
```

HTTP `200` with body `ok` means the webhook works.

### Check a real workflow

Launch a prompt-mode session using a prompt with a Slack phase (for example
`prompts/daily-bug-triage.md`). When the run completes, confirm the message appears in the
target Slack channel and review session output for `Slack: sent` (or `Slack: skipped` if the
webhook was missing).

---

## Workspace vs session scope

| Scope | Supported? | How |
|-------|------------|-----|
| **Workspace** | Yes | Environment Variables page |
| **Session** | Indirect only | All sessions in the workspace share the same env vars; no per-session override in the UI |

**Different channels for different jobs:** create separate Swarmer workspaces, each with its
own `SLACK_WEBHOOK_URL`, and assign sessions accordingly.

**Shared webhook across teams:** one workspace, one webhook — all sessions post to the same
channel.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Prompt logs `SLACK_WEBHOOK_URL is not set` | Var missing or session launched before save | Add var on Environment Variables page; **re-launch** the session (existing sandboxes do not pick up new vars) |
| `403` or `404` from Slack | Revoked or wrong webhook URL | Create a new Incoming Webhook in Slack; update the workspace var |
| `invalid_payload` (HTTP 400) | Malformed JSON from the agent script | Check session raw log for the payload script error |
| Message never arrives, HTTP 200 | Wrong channel or webhook for another workspace | Confirm the webhook’s channel in Slack app settings |
| TUI shows `NOT SET` but UI has the var | Stale sandbox from before the var was added | Stop the session and launch again |

---

## Security notes

- Treat `SLACK_WEBHOOK_URL` like a password — anyone with the URL can post to the channel.
- Swarmer encrypts workspace env vars at rest; restrict Swarmer login and workspace RBAC to
  trusted users (`make grant-workspace-access`).
- Incoming Webhooks cannot edit or delete messages after send; use them for notifications,
  not interactive workflows.
- Do not commit webhook URLs to git or paste them into prompt libraries.

---

## Related documentation

- [USER_GUIDE.md](USER_GUIDE.md) — workspaces, sessions, cron scheduling, access control
- [server-foundation-agent prompts](https://github.com/stolostron/server-foundation-agent/tree/main/prompts) — workflows that use `SLACK_WEBHOOK_URL`
- [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md) — GitHub App auth for repo access in the same workspace
