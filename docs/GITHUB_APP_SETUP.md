# GitHub App Setup Guide

Swarmer supports GitHub Apps as a more secure alternative to Personal Access Tokens (PATs)
for repository access. Instead of storing long-lived user credentials, Swarmer uses the App's
private key to mint short-lived Installation Access Tokens (IATs) server-side — the raw key
never enters a sandbox.

## How it works

1. **At session launch**, Swarmer reads the workspace's GitHub App credentials from the
   encrypted database, builds a signed JWT, and exchanges it for a 1-hour IAT via the
   GitHub REST API.
2. **The IAT is injected** into the OpenShell sandbox as `GH_TOKEN` / `GITHUB_TOKEN` via
   the Gateway provider API — identical to how PATs are injected.
3. **For TUI and server-mode sessions** that can run longer than one hour, a background task
   automatically re-mints the IAT every 50 minutes and updates the provider, keeping the
   token valid for the session's lifetime.
4. **PAT fallback**: if no GitHub App is configured for a workspace, or if IAT minting fails,
   the session falls back to the PAT assigned to that session.

## Switching from PATs

GitHub App and PAT auth are additive, not exclusive:

- Once a GitHub App is configured for a workspace, **all new sessions use the App IAT first**.
- Existing sessions that have a PAT assigned continue to work — the PAT becomes a fallback,
  not a replacement.
- You do not need to remove PATs from existing sessions. They remain as a safety net if the
  App is misconfigured.

---

## Step 1: Create the GitHub App

1. In your GitHub organization, go to **Settings → Developer settings → GitHub Apps**.
2. Click **New GitHub App**.
3. Fill in:
   - **GitHub App name**: e.g., `My Org Swarmer`
   - **Homepage URL**: your Swarmer instance URL
   - **Webhook**: disable (uncheck "Active" under Webhook)
4. Under **Repository permissions**, set:

   | Permission | Level | Why |
   |---|---|---|
   | **Contents** | Read & Write | Clone repos, push commits from agent |
   | **Pull requests** | Read & Write | Open, update, and merge PRs |
   | **Metadata** | Read | Required for all GitHub Apps |
   | **Issues** _(optional)_ | Read & Write | Only needed if agents interact with issues |

5. Under **Where can this GitHub App be installed?**, choose **Only on this account** (your org).
6. Click **Create GitHub App**.

> **Note**: After creation you will be on the App settings page. Keep this page open — you need
> the **App ID** in Step 3.

---

## Step 2: Generate a private key

1. On the GitHub App settings page, scroll down to **Private keys**.
2. Click **Generate a private key**.
3. GitHub downloads a `.pem` file — save it securely. You will paste its contents into Swarmer.

> The private key is the only secret you need. Swarmer encrypts it at rest with Fernet.

---

## Step 3: Install the App on your repositories

1. On the App settings page, click **Install App** in the left sidebar.
2. Click **Install** next to your organization.
3. Choose which repositories the App can access:
   - **All repositories** — easiest; covers future repos automatically.
   - **Only select repositories** — more restricted; select each repo you want agents to access.
4. Click **Install**.

After installation, you are redirected to the installation page. The URL contains the
**Installation ID**: `github.com/organizations/<org>/settings/installations/<installation_id>`.

> Copy this number — you need it in Step 4.

---

## Step 4: Configure Swarmer

1. In Swarmer, navigate to your workspace → **AI Tokens** → **GitHub App** tab.
2. Fill in:
   - **App ID**: the numeric ID from the GitHub App settings page (e.g., `123456`).
   - **Installation ID**: from the installation URL (e.g., `789012`).
   - **Private Key (PEM)**: paste the full contents of the `.pem` file downloaded in Step 2,
     including the `-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE KEY-----` lines.
   - **Share with all workspace users**: check this if you want all users in the workspace to
     benefit from App-based auth (recommended for shared workspaces).
3. Click **Save GitHub App**.

Swarmer encrypts the private key before storing it. The raw PEM is never logged or returned
by the API (`has_private_key: true` confirms it is stored, but the key itself is never exposed).

---

## Verifying the setup

1. Create or launch a session that has a GitHub repo attached.
2. In the session status, look for a successful sandbox creation — if the App is working,
   the session should clone the repo without needing a PAT.
3. In the Swarmer application logs, look for:
   ```
   github_auth: minted IAT for app_id=... installation_id=... expires_at=...
   _do_launch_openshell: using GitHub App IAT for session ...
   ```

If IAT minting fails, Swarmer logs a warning and falls back to the session's assigned PAT
(if any). If neither is available and repos are attached, launch will fail with an error.

---

## Rotating the private key

To rotate the GitHub App private key:

1. In GitHub, generate a new private key (Step 2 above). Do **not** delete the old key yet.
2. In Swarmer → **AI Tokens** → **GitHub App**, paste the new key and click **Save GitHub App**.
3. Confirm the update succeeded (a new `✓ Private key stored` indicator appears).
4. Delete the old key from GitHub App settings.

Running sessions are not affected immediately — they continue using the IAT minted from the
old key until it expires. The refresh loop will pick up the new key on the next re-mint.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` from GitHub | Wrong App ID or expired key | Verify App ID matches the GitHub App settings page |
| `404 Not Found` on IAT endpoint | Wrong Installation ID | Check the URL on the installation page |
| Clone fails with `403` | App not installed on the repo | Install App on the target repository |
| No IAT minted, PAT fallback used | IAT minting failed (check logs) | Look for `github_auth` WARNING lines in Swarmer logs |
| Key rejected as invalid | PEM not copied completely | Ensure the full PEM including header/footer lines is pasted |
