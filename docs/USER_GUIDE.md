# Agent Swarm User Guide

## Overview

Agent Swarm is a FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes. It provides a server-rendered web UI (PatternFly 6 dark theme) for orchestrating multi-agent coding sessions backed by Kubernetes Pods and PVCs.

**Key capabilities:**

- **Workspaces** — each workspace maps 1:1 to a Kubernetes namespace; create, rename, and delete from the UI
- **Secrets** — Fernet-encrypted storage for provider credentials (GCP/Vertex AI, Gemini), GitHub PATs, and OCI pull secrets; auto-synced to Kubernetes Secrets
- **Session lifecycle** — create → launch → monitor → stop → delete sessions backed by Kubernetes Pods and PVCs
- **Three session modes** — Prompt (one-shot), Server (persistent web API), TUI (browser terminal)
- **Git cloning** — init containers clone configured repos into PVC-backed workspaces before the agent starts
- **Live UI** — HTMX polling for session status and output; no page reloads needed
- **Agent tool support** — OpenCode (Go-based) coding agent, with pluggable tooling for future agents
- **MCP server integration** — Model Context Protocol servers per workspace (e.g., Atlassian Jira)
- **Prompt library** — workspace-level prompt library with git-backed folders and per-session picker
- **Cron scheduling** — recurring prompt-mode sessions on a cron schedule
- **REST API** — full `/api/v1/` REST API alongside the HTMX Console

**OpenShift is the recommended cluster type.** Kind is supported for local development and testing.

---

## Prepare Your Environment

### OpenShift (Recommended)

#### Prerequisites

- `oc` CLI installed and logged in to the target cluster
- `cluster-admin` privileges (required for OAuthClient and ClusterRole creation)
- Python 3.11+ (for secret key generation)
- `podman` or `docker`

#### Verify cluster connection

```bash
oc whoami
oc cluster-info
```

Expected: your username and the cluster API URL. If this fails, run `oc login` first.

#### Determine cluster-specific values

```bash
APPS_DOMAIN=$(oc get ingress.config cluster -o jsonpath='{.spec.domain}')
SWARMER_HOST="swarmer.${APPS_DOMAIN}"
OAUTH_HOST=$(oc get route oauth-openshift -n openshift-authentication -o jsonpath='{.spec.host}')
OPENSHIFT_OAUTH_URL="https://${OAUTH_HOST}"
SWARMER_IMAGE="quay.io/jpacker/swarmer:$(cat VERSION)"

# Agent tool image — update this to match your registry
AGENT_IMAGE_OPENCODE="quay.io/jpacker/opencode:0.3.9"

echo "App domain:   ${APPS_DOMAIN}"
echo "Swarmer URL:  https://${SWARMER_HOST}"
echo "OAuth URL:    ${OPENSHIFT_OAUTH_URL}"
echo "Image:        ${SWARMER_IMAGE}"
echo "OpenCode img: ${AGENT_IMAGE_OPENCODE}"
```

Verify the output looks correct before continuing.

### Local Development with Kind

#### Prerequisites

- Python 3.11+ and `pip`
- `kubectl`
- `kind`
- Docker or Podman (`CONTAINER_CMD=podman` to use Podman)
- OpenCode agent container image available locally

#### Two development modes

| Mode | Description | Dashboard URL |
|---|---|---|
| **Hybrid** (hot-reload) | FastAPI runs locally with auto-reload; session pods run inside kind | `http://localhost:8090` |
| **Fully containerized** | Everything runs in kind; one-shot `make kind-deploy` | `http://localhost:8080` |

---

## Install

### Option 1 — OpenShift Deployment (Recommended)

This is the manual step-by-step procedure. For an automated approach, see `make deploy`.

#### Step 1 — Apply shared resources (namespace, RBAC, PVC, model presets)

```bash
oc apply -f k8s/swarmer/namespace.yaml
oc apply -f k8s/swarmer/rbac.yaml
oc apply -f k8s/swarmer/pvc.yaml
oc apply -f k8s/swarmer/configmap.yaml
```

> `configmap.yaml` holds the Claude/Gemini model preset mappings (ACM-37232). Edit it directly
> and re-apply + `oc rollout restart deployment/swarmer -n swarmer` to bump a model ID when
> Vertex AI / Google ship new versions — no code change or image rebuild needed.

#### Step 2 — Create the swarmer secret

```bash
SWARMER_SECRET_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
oc create secret generic swarmer-secret \
  --from-literal=SWARMER_SECRET_KEY="${SWARMER_SECRET_KEY}" \
  -n swarmer --dry-run=client -o yaml | oc apply -f -
```

> **Note:** Each run regenerates the key, invalidating existing sessions. Skip this step on re-deploys if you want to preserve sessions.

#### Step 3 — Apply OpenShift service

```bash
oc apply -f k8s/openshift/service.yaml
```

#### Step 4 — Apply Route

```bash
sed "s|SWARMER_HOST|${SWARMER_HOST}|g" k8s/openshift/route.yaml | oc apply -f -
```

#### Step 5 — Apply OAuthClient

This requires `cluster-admin`.

```bash
sed "s|SWARMER_HOST|${SWARMER_HOST}|g" k8s/openshift/oauth-client.yaml | oc apply -f -
```

#### Step 6 — Apply Deployment

```bash
sed -e "s|SWARMER_IMAGE|${SWARMER_IMAGE}|g" \
    -e "s|OPENSHIFT_OAUTH_URL_VALUE|${OPENSHIFT_OAUTH_URL}|g" \
    -e "s|AGENT_IMAGE_OPENCODE_VALUE|${AGENT_IMAGE_OPENCODE}|g" \
    k8s/swarmer/deployment.yaml | oc apply -f -
```

#### Step 7 — Wait for rollout

```bash
oc rollout status deployment/swarmer -n swarmer --timeout=120s
```

#### Step 8 — Verify and access

```bash
oc get pods -n swarmer
oc get route swarmer -n swarmer
echo "Swarmer is available at: https://${SWARMER_HOST}"
```

Open `https://${SWARMER_HOST}` in a browser. You will be redirected to OpenShift OAuth login.

### Option 2 — Kind Cluster (Fully Containerized)

Best for end-to-end local testing. One command builds the image, creates the cluster, and deploys everything.

```sh
make setup-secret    # generate SWARMER_SECRET_KEY → auth/secret.key
make kind-deploy     # create cluster + build image + load + deploy (idempotent)
```

Dashboard: http://localhost:8080 (via NodePort — no port-forward needed)

Teardown:
```sh
make kind-delete     # deletes the kind cluster and all data inside it
```

### Option 3 — Kind Hybrid (Hot-Reload Dev)

Best for active Python development. FastAPI runs locally with auto-reload; session pods run inside kind.

```sh
make setup-secret          # generate SWARMER_SECRET_KEY → auth/secret.key
make kind-deploy           # create kind cluster + build + load image + deploy (includes OpenShell)
make dev                   # pip install + uvicorn at http://localhost:8090, K8S_IN_CLUSTER=false
```

Dashboard: http://localhost:8090

The `make dev` target automatically sets `K8S_IN_CLUSTER=false` so the local FastAPI process uses your local kubeconfig to talk to the kind cluster.

### Option 4 — Existing Kubernetes Cluster

Push the image to a registry and deploy to your current `kubectl` context.

```sh
make setup-secret
make image-build image-push REGISTRY=your-registry.example.com
make deploy                # applies namespace, RBAC, PVC, service, deployment + installs OpenShell
make connect               # port-forward → http://localhost:8080
```

Dashboard: http://localhost:8080 via port-forward

Teardown:
```sh
make delete                # removes swarmer + OpenShell from the cluster
```

### Option 5 — Kustomize Overlays

Declarative deployment using Kustomize overlays instead of `make`. Two flavors:

#### Flavor comparison

| | cluster-admin | namespace-scoped |
|---|---|---|
| **Permissions** | cluster-admin | namespace editor |
| **Namespace** | Creates `swarmer` | Uses existing namespace |
| **RBAC** | ClusterRole / ClusterRoleBinding | Role / RoleBinding |
| **Workspace isolation** | One namespace per workspace | All workspaces share one namespace |
| **Auth** | OpenShift OAuth + bearer token | Bearer token only |
| **OAuthClient** | Included | Not included |
| **User management** | `make user-token` / `make grant-workspace-access` / `make grant-workspace-create` | Use your existing cluster credentials |

#### Prerequisites

1. `oc` or `kubectl` CLI authenticated to the target cluster
2. A pre-built swarmer container image pushed to a registry accessible by the cluster:
   ```sh
   # Build
   podman build -f Containerfile -t <registry>/<namespace>/swarmer:latest .

   # Push (for OpenShift internal registry)
   oc registry info  # get the registry URL
   podman login <registry> -u $(oc whoami) -p $(oc whoami --show-token) --tls-verify=false
   podman push <registry>/<namespace>/swarmer:latest --tls-verify=false
   ```

#### Deploying with cluster-admin

```sh
# 1. Create the secret key
oc create secret generic swarmer-secret \
  --from-literal=SWARMER_SECRET_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())") \
  -n swarmer --dry-run=client -o yaml | oc apply -f -

# 2. Deploy
oc apply -k kustomize/base/cluster-admin

# 3. Set the image (replace SWARMER_IMAGE placeholder)
oc set image deployment/swarmer swarmer=<your-image> -n swarmer

# 4. Set agent image and OAuth URL
oc set env deployment/swarmer -n swarmer \
  AGENT_IMAGE_OPENCODE=<your-opencode-image> \
  OPENSHIFT_OAUTH_URL=https://$(oc get route oauth-openshift -n openshift-authentication -o jsonpath='{.spec.host}')

# 5. Update OAuthClient redirect URI
SWARMER_HOST=$(oc get route swarmer -n swarmer -o jsonpath='{.spec.host}')
oc patch oauthclient swarmer --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/redirectURIs/0\",\"value\":\"https://${SWARMER_HOST}/auth/callback\"}]"
```

Dashboard: `https://<route-host>`

##### User onboarding (cluster-admin)

```sh
make user-token SA_USER=alice                                  # create user + print token
make grant-workspace-access SA_USER=alice WORKSPACE_NS=team-a  # grant workspace access
```

#### Deploying namespace-scoped (no cluster-admin)

```sh
NAMESPACE=my-namespace

# 1. Create the secret key
oc create secret generic swarmer-secret \
  --from-literal=SWARMER_SECRET_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())") \
  -n $NAMESPACE

# 2. Create an overlay (or copy the example)
cp -r kustomize/overlays/ephemeral kustomize/overlays/my-env

# 3. Edit kustomization.yaml — replace placeholders:
#    - NAMESPACE      → your target namespace
#    - IMAGE_REGISTRY → your registry

# 4. Deploy
oc apply -k kustomize/overlays/my-env
```

#### Differences from Makefile deployment

- **Declarative** — all configuration is in YAML files, not shell variable substitution
- **No Makefile required** — deploy with `oc apply -k` alone
- **Overlay pattern** — environment-specific values (namespace, image, env vars) are separated from the base manifests
- **User onboarding** — `make user-token`, `make grant-workspace-access`, and `make grant-workspace-create` still work alongside Kustomize deployments

---

## Configure

### Environment Variables

Copy `.env.example` to `.env` and adjust as needed:

```sh
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/swarmer.db` | SQLite database path |
| `SWARMER_SECRET_KEY` | _(auto-generated)_ | Fernet encryption key; base64url-encoded 32-byte key |
| `K8S_IN_CLUSTER` | `false` | Set to `true` when running inside a pod |
| `K8S_API_URL` | `https://kubernetes.default.svc` | K8s API server URL (for non-in-cluster deployments) |
| `OPENSHIFT_OAUTH_URL` | _(empty)_ | OpenShift OAuth server URL; leave empty for Kind/K3s |
| `REDIRECT_BASE_URL` | _(empty)_ | Explicit OAuth callback base URL; leave empty to auto-detect |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8080` | Listen port |
| `AGENT_IMAGE` | _(empty)_ | Fallback image for session pods |
| `AGENT_IMAGE_OPENCODE` | _(empty)_ | OpenCode agent container image |
| `DEFAULT_AGENT_TOOL` | `opencode` | Default agent tool when creating sessions |
| `AGENT_IMAGE_PULL_SECRET` | _(empty)_ | Pull secret name in the workspace namespace |
| `AGENT_IMAGE_PULL_POLICY` | `IfNotPresent` | Image pull policy for session pods |
| `K8S_NAMESPACE` | _(empty)_ | Force all workspaces into a single K8s namespace (namespace-scoped mode) |
| `WORKSPACE_ADMIN_USERS` | _(empty)_ | Comma-separated K8s/OIDC usernames that can see and manage every workspace |
| `WORKSPACE_ADMIN_GROUPS` | _(empty)_ | Comma-separated K8s/OIDC groups with the same effect as `WORKSPACE_ADMIN_USERS` |
| `WORKSPACE_CREATE_POLICY` | `all` | `all` — any authenticated user can create a workspace; `admins` — only workspace admins can |
| `MAX_CONCURRENT_AGENTS` | `5` | Global cap on concurrent agent pods; `0` disables the limit |
| `SESSION_RUN_HISTORY_LIMIT` | `100` | Completed prompt-mode runs kept per session in history (with logs); oldest pruned when exceeded; `0` = unlimited |
| `SESSION_RUN_HISTORY_MAX_AGE_DAYS` | `7` | Max age (days) of completed runs kept per session; applied together with `SESSION_RUN_HISTORY_LIMIT` (whichever prunes more wins); `0` = disabled |

> **Ephemeral disk is not configurable (ACM-39804).** Every sandbox pod gets a hardcoded `10Gi` ephemeral-storage compute resource — there is no per-session setting or env var. A per-session dropdown existed under ACM-38184 but was removed: it only bounded this compute resource (container writable layer / unsized emptyDirs), not the `/sandbox` PVC users actually work on, and there is no OpenShell API to size `/sandbox` per session (see `docs/OPENSHELL_LOCAL_SETUP.md` for the gateway-level `workspaceDefaultStorageSize` ceiling, which applies to all sandboxes).

### Secret Key

The `SWARMER_SECRET_KEY` is used for Fernet encryption of all sensitive data at rest (API keys, PATs, credentials) and for deriving the session cookie secret.

**Key source priority:**

1. `SWARMER_SECRET_KEY` environment variable
2. `auth/secret.key` file
3. Auto-generated on first run (saved to `auth/secret.key`)

**Generation:**

```sh
make setup-secret
# or manually:
python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

The key must decode to exactly 32 bytes (base64url-encoded). All sensitive data (API keys, PATs, credentials) is encrypted with Fernet at rest. The session cookie secret is derived separately via `SHA256("session:" + raw_key)`.

> **Warning:** Re-generating the key invalidates all existing encrypted data (credentials, PATs, MCP tokens). Existing records will return empty strings on decryption with a warning log.

### Database

- **SQLite** via `aiosqlite` + SQLAlchemy 2.x async (`AsyncSession`)
- Database file: `data/swarmer.db` (created automatically on first run)
- Schema created via `Base.metadata.create_all` — no Alembic migrations
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent)

```sh
rm -f data/swarmer.db   # delete the SQLite database (forces fresh schema on next start)
```

> **Note:** SQLite supports only a single concurrent writer. The K8s Deployment uses `strategy: Recreate` (not RollingUpdate). Only one replica is safe.

### Agent Images

Agent container images are built from the repository's Containerfiles:

| Image | Containerfile | Base | UID |
|---|---|---|---|
| Swarmer dashboard | `Containerfile` | UBI10 `python-312-minimal` | 1001 |

**Building:**

```sh
make image-build           # Build swarmer image (depends on sync-images)
```

> **Note:** The OpenCode agent image is built from the `stolostron/agent-containers` repository, not this Makefile.

**Pushing:**

```sh
make image-push REGISTRY=your-registry.example.com
```

**Syncing agent image refs into `.env`:**

The `sync-images` target reads `REGISTRY` and `IMAGE_TAG` from `.push-defaults` and updates `AGENT_IMAGE_OPENCODE` in `.env`:

```sh
make sync-images
```

> **Note:** `image-build` depends on `sync-images`, which requires `.push-defaults` to exist. Use `SILENT=1` to skip the interactive version prompt: `make image-build SILENT=1`.

PVCs must be group-0 writable for the non-root UID 1001 user in the swarmer container.

### Access Control

Authentication uses Kubernetes ServiceAccount bearer tokens — not passwords. **Authorization**
(which workspaces a user can see) is a database-backed ACL (ACM-41659), not Kubernetes RBAC —
a workspace no longer requires a dedicated K8s namespace or RoleBinding to grant access.

#### Issuing login tokens

Creates a Kubernetes ServiceAccount for the user (if it doesn't exist) and prints a bearer token they paste into the Swarmer login page:

```sh
make user-token SA_USER=alice
make user-token SA_USER=alice TOKEN_DURATION=24h   # default: 8h
```

Share the printed token with the user — it expires after `TOKEN_DURATION`. OpenShift OAuth / OIDC
users skip this step and authenticate via the OAuth flow instead (see below).

#### Granting workspace access

Self-service, no `kubectl` required: open the workspace in the UI, go to the **Members** tab,
and add the user's exact K8s username — `system:serviceaccount:<ns>:<name>` for a ServiceAccount
token, or their OpenShift OAuth/OIDC username for OAuth logins. The field suggests candidates from
`GET /api/v1/users`, which merges three sources: people you already share a workspace with (global
admins see every known username), every OpenShift `User` object, and every ServiceAccount
`make user-token SA_USER=<name>` would create/use — but it always remains free-text so you can
invite someone who hasn't logged in yet. Suggestions are rendered as both clickable pills and a
native `<datalist>` on the input. Only the workspace owner or a configured admin can add or remove
members. Equivalent via the API:

```sh
curl -sX POST "$SWARMER_URL/api/v1/workspaces/<id>/members" \
  -H "Authorization: Bearer <owner-or-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice"}'
```

A user with no workspace grants (not an owner, member, or admin of any workspace) can log in but
will see no workspaces.

#### Allowing a user to create new workspaces

By default (`WORKSPACE_CREATE_POLICY=all`), any authenticated user can create a workspace and
becomes its owner. To restrict creation to admins, set `WORKSPACE_CREATE_POLICY=admins` and list
admins via `WORKSPACE_ADMIN_USERS` (comma-separated K8s usernames) and/or `WORKSPACE_ADMIN_GROUPS`
(comma-separated groups) in the deployment environment. Workspace admins can see and manage every
workspace, not just ones they own or are a member of.

#### Global Admins — simple setup

Two ways to grant admin rights, and you can mix both:

- **Self-service:** the first user to log in sees a **"Become the first Admin"** button on the
  Workspaces page (or `/admins`) — one click, works only while zero admins exist. Once bootstrapped,
  admins add/remove other admins from `/admins` (or `POST`/`DELETE /api/v1/admins`).
- **Declarative:** `WORKSPACE_ADMIN_USERS` / `WORKSPACE_ADMIN_GROUPS` env vars (see above) — always
  take effect, no bootstrap step needed, ideal for GitOps-managed deployments.

`GET /api/v1/me` returns `{username, is_admin, can_create_workspace, admin_bootstrap_available}` —
the Console uses it to render admin-gated UI without duplicating ACL logic.

#### Upgrading from an older Swarmer version

Nobody needs to be manually re-added to a workspace they already had access to. On first startup
after upgrading, Swarmer automatically:

1. Backfills `workspace_members` and each workspace's `owner_id` from existing per-user records
   already in the database (AI provider credentials, GitHub PATs, MCP servers, GitHub Apps) —
   plain SQL, runs as part of `migrate_db()`.
2. Mirrors any legacy `make grant-workspace-access` K8s RoleBinding grants (bound to the
   `swarmer-user` ClusterRole) into the same table. Best-effort: skipped per-workspace on any K8s
   error, and never blocks startup.
3. For shared-namespace deployments (`K8S_NAMESPACE` set), every authenticated user keeps access
   to every workspace — unchanged from before, since that deployment flavor already has a single
   shared trust boundary.

A workspace left with no recoverable owner (no prior secrets/PATs and no RoleBinding grants) stays
open to any authenticated user — the first person to rename it, delete it, or add/remove a member
claims ownership automatically. Nothing is ever orphaned or locked out.

#### Typical onboarding flow

```sh
make user-token SA_USER=alice   # 1. create user + print token, share with alice
# 2. alice logs in and either creates her own workspace, or an existing
#    workspace owner adds her via the Members tab / API above.
```

> Deprecated: `make grant-workspace-access` / `make grant-workspace-create` (Kubernetes namespace
> RoleBindings) are kept only for existing automation — Swarmer no longer consults K8s RBAC for
> workspace authorization. Use the Members tab / `WORKSPACE_ADMIN_USERS` / `WORKSPACE_ADMIN_GROUPS`
> / `WORKSPACE_CREATE_POLICY` above instead.

#### OpenShift OAuth

When `OPENSHIFT_OAUTH_URL` is set, a "Sign in with OpenShift" button appears on the login page. Users authenticate via the OpenShift OAuth implicit grant flow — no token pasting required. The callback captures the token from the URL fragment client-side via `/auth/callback`. Grant workspace access to these users the same way as any other user — via the Members tab / API (their username is their OpenShift OAuth/OIDC identity).

---

## Usage

### Workspaces

A workspace is a logical grouping for sessions and secrets, backed by the database (ACM-41659) —
it no longer maps to a dedicated Kubernetes namespace. Access is controlled by the workspace's
owner, its `workspace_members`, and any configured workspace admins (see Access Control above).
`K8S_NAMESPACE` (namespace-scoped deployment) additionally forces all workspaces to share one K8s
namespace for the handful of legacy per-workspace K8s Secret features (pull secrets) that still
use one.

- **Create** — creates a new workspace in the database; the creator becomes its owner. No K8s namespace is created.
- **Rename** — updates the display name
- **Delete** — removes the workspace from the database (and best-effort cleans up its K8s namespace, if a pull secret ever created one)

All resources (sessions, secrets, repos, MCP servers) are scoped to a workspace. Users only see workspaces they own, are an explicit member of, or — for workspace admins — every workspace (see Access Control above).

### Secrets & Credentials

The Secrets page has three tabs:

#### Provider Credentials

Stores credentials for AI model providers. All values are Fernet-encrypted at rest and synced to Kubernetes Secrets when a session is launched.

| Field | Description |
|---|---|
| GCP Project | Google Cloud project ID for Vertex AI |
| Vertex Location | Vertex AI region (e.g., `us-central1`) |
| ADC JSON | Application Default Credentials JSON for Vertex AI |
| Google API Key | API key for Google Gemini (AI Studio) |
| Anthropic API Key | API key for Anthropic Claude (direct) |
| OpenAI API Key | API key for OpenAI models |

> **Note:** Despite the legacy model name `OpencodeSecret`, this stores credentials for AI providers used by OpenCode.

#### GitHub PATs

Personal access tokens for HTTPS git authentication. Each PAT can have an optional org scope. PATs are Fernet-encrypted at rest and synced to K8s Secrets for use by git-init containers.

#### Pull Secrets

Image pull secret name for private container registries. Specify the name of an existing K8s Secret in the workspace namespace.

#### Extra environment variables

Workspace-scoped key-value pairs injected into every sandbox session at launch. Managed from
**Environment Variables** on the workspace sessions page (`/workspaces/{id}/env-vars`).

- Stored in the Swarmer database, **Fernet-encrypted at rest**, and passed to the agent process
  via OpenShell at session launch
- Applies to all sessions in the workspace (prompt, server, TUI, and cron-scheduled runs)
- Common uses: `SLACK_WEBHOOK_URL` for workflow notifications, `SKIP_SLACK` to disable Slack
  for a workspace, or other secrets your agent prompts expect

See [SLACK_NOTIFICATIONS.md](SLACK_NOTIFICATIONS.md) for Slack Incoming Webhook setup.

### Git Repositories

Repositories are configured per-session via the session configuration UI:

- **Add** repos by URL; they are cloned via init containers into the PVC-backed workspace before the agent starts
- Git auth uses the workspace's GitHub PAT for HTTPS cloning (injected as `GIT_CREDENTIALS` in the init container)
- Repo context is injected into `AGENTS.md` (TUI/server modes) and prompt text (prompt mode) as a structured markdown table

### Sessions

#### Session Modes

##### Prompt Mode

One-shot execution: runs the agent with a prompt, streams output, and the pod exits when done.

- `restartPolicy: Never` — pod exits after the agent finishes
- Auto-cleaned by the log poller background task
- If `persist=False`, the PVC is deleted on successful completion
- Supports cron scheduling for recurring runs (see [Cron Scheduling](#cron-scheduling))
- Command: runs with `--continue` flag (falls back to without if session state does not exist)

##### Server Mode

Persistent execution: the agent runs in server mode with an HTTP API.

- `restartPolicy: Always` — pod runs indefinitely
- Creates a ClusterIP Service (+ OpenShift Route if available)
- Dashboard proxies HTTP/WS/SSE to the agent's own web UI (via Route on OpenShift, or sub-path proxy elsewhere)

##### TUI Mode

Persistent terminal: the pod runs `sleep infinity` and the user connects via a full browser terminal.

- `restartPolicy: Always` — pod runs indefinitely
- xterm.js browser terminal connected via WebSocket PTY proxy (`kubectl exec` under the hood)
- OSC 52 clipboard support — clipboard copy operations in the pod reach the user's browser clipboard
- One-time UUID auth tokens prevent unauthorized WebSocket connections

#### Session Lifecycle

Sessions progress through these phases:

```
idle → pending → running → succeeded / failed / stopped
```

| Phase | Description |
|---|---|
| `idle` | Created but not launched |
| `pending` | Launch initiated; PVC + pod being created |
| `running` | Pod is active |
| `succeeded` | Pod completed successfully (prompt mode) |
| `failed` | Pod exited with an error |
| `stopped` | User stopped the session |

**Launch:** Creates a PVC (`session-{id}-{suffix}`), builds the pod spec with init containers (git clone, config setup), and creates the pod.

**Monitor:** Live status polling via HTMX; log streaming for prompt-mode sessions via the log poller background task.

**Stop:** Deletes the pod. If `persist=False`, also deletes the PVC.

**Delete:** Removes the session from the database.

### Agent Tools

Agent Swarm uses OpenCode as its agent tool via the Strategy pattern (`AgentToolStrategy` in `swarmer/agent_tools/__init__.py`), which implements image selection, config generation, model options, pod command construction, and K8s resource layout. The strategy interface is pluggable, so additional agent tools can be added in the future.

#### OpenCode

Go-based AI coding agent ([opencode.ai](https://opencode.ai)).

**Supported providers:**

| Provider | Credential Required | Model Format |
|---|---|---|
| Google Vertex AI (Anthropic Claude) | ADC JSON + GCP Project | `google-vertex-anthropic/claude-sonnet-5@default` |
| Google Gemini (AI Studio) | Google API Key | `google/gemini-3.6-flash` |

**Model format:** `provider/model@version` (e.g., `google-vertex-anthropic/claude-sonnet-5@default`)

**Modes:**

- **Prompt** — one-shot: `opencode run --model <model> --continue <prompt>` (falls back without `--continue`)
- **Server** — HTTP API: `opencode serve --hostname 0.0.0.0 --port 4096`
- **TUI** — interactive terminal: `opencode` (launched via `sleep infinity` pod, user attaches)

Config written to `/workspace/.config/opencode/opencode.json` at pod startup.

#### Model Selection

- The primary UX is two family-level **presets** — **Claude** and **Gemini** — selected via
  radio pills at the top of the Model field. Each preset maps to three roles configured in
  `Settings` (`swarmer/config.py`), so they can be changed without code changes:
  - **plan** — stronger-reasoning model used by the opencode plan agent (requires
    `OPENCODE_EXPERIMENTAL_PLAN_MODE=true`, enabled by default)
  - **build** — the model used for `opencode run` / the coding agent (this is what session.model
    resolves to for policy/network purposes)
  - **small** — title generation / housekeeping model
- An **Advanced** `<details>` toggle reveals the full individual model dropdown (grouped by
  provider) for users who want to pick a specific model instead of a preset — session.model
  stores the raw `provider/model@version` string in this case
- Both presets and individual models are **always listed**, even when the backing provider
  isn't configured — unavailable choices are shown disabled with an inline error (e.g. "Vertex
  AI not configured — add credentials in Secrets") instead of silently disappearing
- Default model auto-selected based on available credentials:
  - ADC configured → **Claude** preset
  - Gemini API key only → **Gemini** preset

### MCP Servers

MCP (Model Context Protocol) server configurations are managed per workspace.

- **Pre-configured catalog** includes Atlassian Jira (Rovo) with API token authentication (server URL, token, email)
- Tokens encrypted at rest via Fernet, mounted as K8s secret environment variables (`MCP_TOKEN_<SLUG>`)
- Enabled MCP servers are injected into agent configs at launch: added to the `mcp` section of `opencode.json` as local command servers
- Jira MCP uses the `jira-mcp-server` binary with `JIRA_SERVER_URL`, `JIRA_ACCESS_TOKEN`, and `JIRA_EMAIL` environment variables

### Prompt Library

Workspace-level prompt library with git-backed folders:

- Configure prompt sources (git URLs) per workspace
- Recursive `.md` file caching from configured URL sources
- Per-session prompt picker with live preview in the UI
- Composable **Additional Instructions** layer: free-text instructions always prepended to the base prompt selected from the library
- Prompts are injected into the agent command (prompt mode) or AGENTS.md (TUI/server modes)

### Cron Scheduling

Prompt-mode sessions can be configured with a cron schedule for recurring execution:

- **Schedule format:** standard cron expression (e.g., `0 */6 * * *` for every 6 hours)
- **Background loop:** an asyncio task in `scheduler.py` checks every 30 seconds for due sessions
- **Atomic claim:** uses `UPDATE ... RETURNING` to atomically claim due sessions, preventing duplicate launches
- **Failure handling:** on launch failure, resets phase to `idle` and advances `cron_next_run` to the next occurrence
- Only prompt-mode sessions support cron — server and TUI modes are persistent and don't need scheduling

### Patch Generation

Generate git diffs from running session pods:

- Executes `git diff` (or `git diff origin/{branch}` for working branches) via `kubectl exec`
- AI-generated commit messages via Vertex AI Claude, Anthropic API, or Gemini API (falls back to a simple file-list summary)
- Download as `.patch` files for local application

---

## Teardown

### OpenShift / Kubernetes (generic)

```sh
make delete    # removes swarmer resources + uninstalls OpenShell
```

Or manually with `oc`:

```bash
oc delete -f k8s/swarmer/deployment.yaml --ignore-not-found
oc delete -f k8s/swarmer/configmap.yaml --ignore-not-found
oc delete -f k8s/openshift/service.yaml --ignore-not-found
oc delete route swarmer -n swarmer --ignore-not-found
oc delete oauthclient swarmer --ignore-not-found
oc delete -f k8s/swarmer/pvc.yaml --ignore-not-found
oc delete secret swarmer-secret -n swarmer --ignore-not-found
oc delete -f k8s/swarmer/rbac.yaml --ignore-not-found
oc delete -f k8s/swarmer/namespace.yaml --ignore-not-found
```

### Kind

```sh
make kind-delete
```

### Kustomize

```sh
# cluster-admin flavor
oc delete -k kustomize/base/cluster-admin

# namespace-scoped overlay
oc delete -k kustomize/overlays/my-env
```

---

## Appendix

### Setup guides

| Guide | Description |
|-------|-------------|
| [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md) | GitHub App auth instead of PATs |
| [SLACK_NOTIFICATIONS.md](SLACK_NOTIFICATIONS.md) | Slack webhooks for workspace sessions |
| [OPENSHELL_LOCAL_SETUP.md](OPENSHELL_LOCAL_SETUP.md) | Local OpenShell gateway development |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal architecture reference |

### Makefile Reference

All targets can be listed with `make help`. Run `make lint` to check code style and `make test` to run the test suite.

#### Developer Tooling

| Target | Description | Key Variables |
|---|---|---|
| `setup-secret` | Generate `SWARMER_SECRET_KEY` → `auth/secret.key` | |
| `dev` | `pip install` + Uvicorn at `localhost:8090` with `--reload`, `K8S_IN_CLUSTER=false` | |
| `lint` | `ruff check swarmer/` | |
| `test` | `pytest tests/ -q --ignore=tests/test_ui_patternfly.py` | |
| `sync-images` | Sync `AGENT_IMAGE_*` in `.env` from `.push-defaults` | `AC_DEFAULTS` |

> **Reset database:** `rm -f data/swarmer.db` — fresh schema is created on next start.

#### Container Image

| Target | Description | Key Variables |
|---|---|---|
| `image-build` | Build the swarmer container image | `REGISTRY`, `SILENT` |
| `image-push` | Push image to registry | `REGISTRY` |

#### Deploy / Delete

| Target | Description | Key Variables |
|---|---|---|
| `deploy` | Deploy swarmer + OpenShell to current `kubectl` context (auto-detects OpenShift) | `IMAGE_REF`, `NAMESPACE`, `SILENT` |
| `delete` | Remove swarmer + OpenShell from cluster | `NAMESPACE` |
| `connect` | Port-forward dashboard to `localhost:$(LOCAL_PORT)` | `NAMESPACE`, `LOCAL_PORT` |
| `connect-openshell` | Port-forward OpenShell gateway gRPC port | `OPENSHELL_NAMESPACE` |
| `status` | Show OpenShell and swarmer deployment status | `NAMESPACE` |

#### Kind (Local Dev)

| Target | Description | Key Variables |
|---|---|---|
| `kind-deploy` | One-shot: create cluster + build + load image + deploy (includes OpenShell) | `KIND_CLUSTER` |
| `kind-delete` | Delete the kind cluster and all data | `KIND_CLUSTER` |

#### User Management

| Target | Description | Key Variables |
|---|---|---|
| `user-token` | Issue a K8s login token for a user | `SA_USER`, `TOKEN_DURATION` (default `8h`) |
| `grant-workspace-access` | **[Deprecated]** K8s namespace RoleBinding — no longer read by Swarmer (ACM-41659); use the Members tab / API instead | `SA_USER` or `OIDC_USER`, `WORKSPACE_NS` |
| `grant-workspace-create` | **[Deprecated]** K8s ClusterRoleBinding — no longer read by Swarmer (ACM-41659); use `WORKSPACE_CREATE_POLICY` instead | `SA_USER` or `OIDC_USER` |
| `grant-workspace` | Deprecated alias for `grant-workspace-access` | `SA_USER` or `OIDC_USER`, `WORKSPACE_NS` |

#### Key overridable variables

| Variable | Default | Description |
|---|---|---|
| `IMAGE` | `swarmer` | Image name |
| `IMAGE_TAG` | `$(cat VERSION)` | Image tag (from `VERSION` file) |
| `REGISTRY` | _(empty)_ | Container registry prefix |
| `CONTAINER_CMD` | `podman` | Container runtime (`podman` or `docker`) |
| `KIND_CLUSTER` | `swarmer` | Kind cluster name |
| `NAMESPACE` | `swarmer` | Kubernetes namespace |
| `SA_USER` | _(required)_ | ServiceAccount username for token/grant targets (mutually exclusive with `OIDC_USER`) |
| `OIDC_USER` | _(required)_ | OpenShift OAuth/OIDC `User` name for grant targets — use instead of `SA_USER` for users who don't log in with a ServiceAccount token (mutually exclusive with `SA_USER`) |
| `WORKSPACE_NS` | _(required)_ | Workspace namespace for grant-workspace-access |
| `TOKEN_DURATION` | `8h` | Token validity duration |
| `SILENT` | _(empty)_ | Set to `1` to skip interactive prompts |

### Troubleshooting

#### Secret key regeneration invalidates data

If you regenerate the `SWARMER_SECRET_KEY`, all existing Fernet-encrypted data (credentials, PATs, MCP tokens) becomes unreadable. Decryption returns empty strings with a warning log. Re-enter credentials after key rotation.

#### SQLite single-writer limitation

SQLite does not support concurrent writers. The K8s Deployment uses `strategy: Recreate` (not `RollingUpdate`). Only one replica is safe. If you see database locking errors, ensure only one swarmer pod is running.

#### `sync-images` requires `.push-defaults`

The `image-build` target depends on `sync-images`, which reads `REGISTRY` and `IMAGE_TAG` from `.push-defaults`. If this file does not exist, the build fails. Create `.push-defaults` with:

```
REGISTRY=your-registry.example.com
IMAGE_TAG=0.1.0
```

#### PVC permissions for non-root containers

The swarmer container runs as non-root UID 1001. PVC root directories must be group-0 writable for the `1001:0` user/group combination. If sessions fail with permission errors, check PVC ownership:

```sh
oc exec -it <pod> -- ls -la /data
```

#### Debug commands

```sh
oc get pods -n swarmer                  # List swarmer pods
oc logs deployment/swarmer -n swarmer   # View swarmer logs
oc get route swarmer -n swarmer         # Check route
rm -f data/swarmer.db                   # Reset database (fresh schema on next start)
```
