# OpenShell Local Development Setup

Step-by-step guide to running Swarmer with OpenShell 0.1.4 and Agent Sandbox v1.0.4, using either a locally hosted or remote/hosted gateway.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| `kind` | any | Only needed for local kind setup (`kind version`) |
| `kubectl` | any | Must match cluster API version |
| `helm` | 3.8+ | Required for OCI chart support |
| `oc` | any | OpenShift only — grants SCC policies |
| Python | 3.12 | `python3 --version` |
| `openshell` CLI | 0.1.4 | `openshell --version` — use the exact pinned gateway release |

> **Version matters.** The Python SDK, CLI, and gateway use the OpenShell 0.1.4 API. Keep the `openshell` CLI at the same release as `OPENSHELL_VERSION` in the Makefile.

## Running Swarmer inside OpenShell

The `SWARMER_RUNTIME_MODE=openshell` application mode is for a Swarmer instance
running inside an existing OpenShell sandbox. It skips Kubernetes client
initialization, the legacy Kubernetes workspace-member migration, and
Kubernetes image-availability probes. The regular Kubernetes deployment remains
the default and keeps its existing TokenReview authentication.

For hosted identity, configure both `OPENSHELL_OIDC_ISSUER` and
`OPENSHELL_OIDC_AUDIENCE`. Swarmer validates the token signature using the
issuer's HTTPS discovery document and JWKS, and checks issuer, audience,
expiration, issued-at time, and subject. Tokens without these verified claims
are rejected. JWKS must be served by the issuer origin unless an additional
HTTPS origin is explicitly listed in `OPENSHELL_OIDC_JWKS_ALLOWED_ORIGINS`.
The stable JWT `sub` is the ACL username; `OPENSHELL_OIDC_GROUPS_CLAIM` selects
the groups claim (including dot-separated nested claims).
For standalone bootstrap, mount the administrator token as a
file and set `SWARMER_ADMIN_TOKEN_FILE` to that mounted file; do not place the
token in an ordinary environment variable or command-line argument. The token
is compared in constant time and grants the configured bootstrap administrator
identity. Rotate or revoke it by replacing/removing the mounted secret.

Swarmer's identity to the Gateway is separate from users' login tokens. For a
hosted gateway, configure a dedicated OIDC service account using the
`OPENSHELL_GATEWAY_OIDC_ISSUER`, `OPENSHELL_GATEWAY_OIDC_CLIENT_ID`,
`OPENSHELL_GATEWAY_OIDC_AUDIENCE`, and optional `OPENSHELL_GATEWAY_OIDC_SCOPES`
settings. Set `OPENSHELL_GATEWAY_OIDC_CLIENT_SECRET_FILE` to a mounted secret
file. The OpenShell Python SDK obtains and renews client-credentials tokens
lazily; user CLI tokens, refresh tokens, and personal mTLS keys are not needed
inside the Swarmer sandbox. Restrict the service account's gateway roles and
workspace membership to only the workspace(s) Swarmer manages.

OpenShell mode does not need a Kubernetes API URL or Kubernetes credentials.
Kubernetes-only registry pull-secret and namespace-management features are not
available in this runtime; the pull-secret tab is hidden and legacy REST
mutations are rejected. Agent sandbox image resolution and pulling are handled
by the selected OpenShell gateway. User autocomplete omits Kubernetes and
OpenShift directory results.

## Clean gateway/session migration

Swarmer is pinned to OpenShell 0.1.4 and Agent Sandbox v1.0.4. The gateway API,
protobufs, and sandbox runtime are a clean cutover; old gateway clients and
sandbox instances are not supported. Before upgrading an existing deployment,
stop active sessions while the old gateway is still available and save any
provider configuration needed to re-enter. Deploy the pinned gateway and CRDs,
restart Swarmer, and launch new sessions. Re-enter provider credentials through
Swarmer so secrets are stored on the gateway; do not move credentials into
Swarmer environment variables or sandbox specifications. Swarmer imports its
required GitHub, Google, and application provider profiles on each gateway.

## Deployment: `make deploy`

`make deploy` handles the full OpenShell lifecycle — install, SCC grants, cert extraction, and gateway registration — in a single idempotent command. You do not need to run any OpenShell steps manually.

Use `make update-deps` to discover the latest stable OpenShell and Agent Sandbox
releases and update the repository pins before deploying. Swarmer records the
last observed version of each gateway. If a gateway version changes, its
OpenShell-backed AI provider state is treated as invalid and the corresponding
configured flags are cleared; raw AI credentials are never stored by Swarmer.

What `make deploy` does:

1. **Installs Agent Sandbox CRDs** from the pinned `AGENT_SANDBOX_VERSION` release manifest.
2. **Installs or upgrades OpenShell** from the pinned OCI chart. The `OPENSHELL_WORKSPACE_STORAGE` setting controls the per-sandbox `/sandbox` PVC size; it is gateway-wide, and a new size applies to newly created sandboxes.
3. **Grants OpenShift SCCs** (if `oc` is on PATH — no-op on plain Kubernetes):
   - `anyuid` for `agent-sandbox-controller` in `agent-sandbox-system`
   - `anyuid` and `privileged` for both `openshell` and `openshell-sandbox` service accounts
   - Sandbox pods require `NET_ADMIN`, `SYS_ADMIN`, `SYS_PTRACE`, and `SYSLOG` capabilities; `anyuid` alone is insufficient
   - These grants are applied on every `make deploy` run (both install and re-deploy) so they survive namespace recreation
4. **Extracts mTLS client certs** from the `openshell-client-tls` secret into `auth/openshell/`
5. **Registers the gateway** via `make openshell-register` — writes `~/.config/openshell/gateways/<name>/metadata.json` with the gateway endpoint and copies the mTLS certs
6. **Deploys Swarmer** to the cluster with the extracted certs injected as a K8s secret

### Auth model

The Helm chart is deployed with `server.auth.allowUnauthenticatedUsers=true`. The gateway enforces mTLS mutual authentication (client cert required); the JWT bearer token layer is disabled. This is safe because the gateway is only reachable via `localhost` through a kubectl port-forward — it is not publicly accessible.

Leave `OPENSHELL_BEARER_TOKEN` blank in `.env`.

---

## Quick Start: Local kind cluster

```sh
make setup-secret    # only needed once — generates auth/secret.key
make kind-deploy     # create cluster + build image + deploy (includes OpenShell)
```

Verify:

```sh
make status
```

---

## Quick Start: Existing cluster (Kubernetes or OpenShift)

Point `kubectl` at your cluster, then:

```sh
make setup-secret    # only needed once
make deploy          # install/update OpenShell + Swarmer, extract certs, register gateway
```

On OpenShift, ensure `oc` is on your PATH so the SCC grants are applied automatically.

---

## Remote or hosted gateway

Use this path when Swarmer should connect to a gateway managed outside the
Swarmer cluster. Create or edit a workspace and select **Custom OpenShell
Gateway**. Enter the HTTPS gateway URL (host and optional port; no URL path),
choose the gateway's configured authentication mode, fill in its OIDC issuer and
client details or bearer token, then use **Test Gateway Connection** before
saving. OIDC over HTTPS is recommended for hosted gateways. Use `None` only for
a deliberately unauthenticated gateway on a trusted private network.

For local development against a remote gateway, set `OPENSHELL_GATEWAY_URL` and
the matching `OPENSHELL_BEARER_TOKEN` or TLS settings in the untracked `.env`.
Publicly trusted HTTPS endpoints use system CA roots. Configure
`OPENSHELL_TLS_CA` (and client certificate/key when required) for a private CA
or mTLS gateway. Never commit tokens, private keys, or certificate contents.

Remote gateways must be able to pull the configured agent image. Swarmer imports
the pinned provider profiles on each gateway before creating providers, and
continues to store provider credentials on the gateway. The session lifecycle,
pagination, cleanup, and service exposure use the same OpenShell 0.1.4 API for
local and remote gateways.

---

## Connecting to the OpenShell gateway

### `make connect-openshell` — start port-forwards

Run in a **separate terminal** (keep it running while using Swarmer):

```sh
make connect-openshell
```

This reads every registered gateway from `~/.config/openshell/gateways/*/metadata.json` and starts a `kubectl port-forward` for each one — one per registered cluster. The port used for each gateway is the one stored in its `metadata.json` (set during `make openshell-register`).

Example output:

```
* [my-cluster] forwarding localhost:17671 → openshell:8080  (context: my-cluster)

Active gateway : my-cluster
Press Ctrl-C to stop all port-forwards.
```

If a port-forward dies unexpectedly, a `[warn]` message is printed. Re-run `make connect-openshell` to restart it.

### `make openshell-register` — register or refresh a gateway

Run after `make deploy` (it is called automatically) or after switching kubectl contexts:

```sh
make openshell-register
```

What it does:

- Derives a stable gateway name from the current kubectl context
- **On first registration**: picks a free port starting from `OS_LOCAL_PORT` (default 17671), avoiding ports already claimed by other registered gateways or currently bound on localhost. Calls `openshell gateway add https://localhost:<port> --local --name <name>` to write `metadata.json`
- **On refresh**: reuses the **existing port** from `metadata.json` without re-probing. This prevents drift between the metadata and the port-forward started by `make connect-openshell`
- Copies the latest mTLS certs from `auth/openshell/` into `~/.config/openshell/gateways/<name>/mtls/`
- Sets the registered gateway as the active gateway

> **Port stability**: `make connect-openshell` always port-forwards to the port in `metadata.json`. `make openshell-register` preserves that port on refresh. Only fresh registrations (no existing `metadata.json`) pick a new port.

---

## Using the `openshell` CLI

Once `make connect-openshell` is running, you can use the `openshell` CLI directly against the live gateway.

### Select the active gateway

```sh
openshell gateway list          # list all registered gateways
openshell gateway select <name> # set the active gateway
openshell status                # verify connection (shows gateway URL and version)
```

### Inspect providers and sandboxes

```sh
openshell provider list         # list credential providers registered on the gateway
openshell sandbox list          # list active sandboxes
openshell sandbox get <name>    # detailed sandbox info (phase, policy, providers)
```

### Open an interactive terminal with `openshell term`

`openshell term` opens a TUI that lets you browse sandboxes and connect to them interactively:

```sh
openshell term
```

The TUI shows all sandboxes on the active gateway. Select a sandbox and press Enter to open an interactive shell inside it.

> **Prerequisite**: `make connect-openshell` must be running and the active gateway must be set (`openshell status` should show `Connected`).

---

## Configure `.env` for local dev server

After `make deploy`, add these to your `.env`:

```sh
OPENSHELL_GATEWAY_URL=https://localhost:17671
OPENSHELL_TLS_CERT=auth/openshell/tls.crt
OPENSHELL_TLS_KEY=auth/openshell/tls.key
OPENSHELL_TLS_CA=auth/openshell/ca.crt
OPENSHELL_BEARER_TOKEN=
```

The port (`17671` above) must match what is in `~/.config/openshell/gateways/<name>/metadata.json`. Check with:

```sh
cat ~/.config/openshell/gateways/*/metadata.json | python3 -m json.tool
```

Copy from `.env.example` if you don't have a `.env` yet:

```sh
cp .env.example .env
# edit the five OPENSHELL_* lines above
```

---

## Start the Swarmer dev server

```sh
make dev    # pip install + uvicorn at http://localhost:8090 with --reload
```

In a second terminal:

```sh
make connect-openshell   # keep this running
```

---

## Smoke test

Verify the full stack before testing through the UI:

```sh
python3 scripts/openshell_smoke_test.py
```

Expected output:

```text
Connecting to localhost:17671 ...
Creating sandbox ...
Created sandbox: <name>
Waiting for ready (first run may take ~3 min for image pull) ...
Running 'echo hello' ...
Exec result: ExecResult(exit_code=0, stdout='hello\n', stderr='')
Deleting sandbox ...
Deleted: True
OK
```

---

## Launch a session through the UI

1. Open `http://localhost:8090` and log in (use `make user-token SA_USER=<name>` for a token)
2. Create a workspace
3. Add a session and click **Launch**
4. The session should reach `running` — Swarmer creates an OpenShell sandbox, not a K8s pod
5. Confirm: `kubectl get sandboxes -n openshell` and `openshell sandbox list`

---

## Teardown

```sh
# Stop Swarmer dev server (Ctrl-C)
# Stop port-forwards (Ctrl-C in the connect-openshell terminal)
make delete         # uninstall Swarmer + OpenShell from cluster
make kind-delete    # delete the kind cluster entirely (kind only)
```

---

## Troubleshooting

**`helm upgrade` fails with "OCI registry not found"**
→ Ensure you can reach `ghcr.io`. Test with: `helm pull oci://ghcr.io/nvidia/openshell/helm-chart --version <version>`

**`gRPC Connection refused on localhost:17671`**
→ The port-forward is not running. Start it with `make connect-openshell` in a separate terminal.

**`openshell status` shows "No active gateway"**
→ Run `openshell gateway select <name>` or re-run `make openshell-register`.

**`gRPC UNAUTHENTICATED: missing authorization header`**
→ The gateway was deployed without `allowUnauthenticatedUsers=true`. Re-run `make deploy` (idempotent — upgrades the Helm release with the correct flag).

**`gRPC UNAUTHENTICATED: invalid token`**
→ A bearer token is set in `.env` that the gateway is trying to validate as a sandbox session token. Clear `OPENSHELL_BEARER_TOKEN=` in `.env`.

**Port-forward dies immediately (`rc=1`)**
→ Check if the port is already in use: `ss -tlnp | grep 1767`. The metadata port may conflict with the local `openshell-gateway` daemon (which binds 17670). Re-run `make openshell-register` to re-check; the port is preserved from `metadata.json` on refresh.

**`metadata.json` port doesn't match the port-forward port**
→ Run `make openshell-register` to refresh. The register target now reuses the existing port rather than re-probing, so drift only occurs if `metadata.json` was created by an older version. If needed, manually edit the port in `metadata.json`:
```sh
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.config/openshell/gateways'
for m in p.glob('*/metadata.json'):
    d = json.loads(m.read_text())
    print(m.parent.name, d.get('gateway_endpoint'))
"
```

**Sandbox pods stuck in `Provisioning` / no pods created (OpenShift)**
→ SCC grants are missing. Run:
```sh
oc adm policy add-scc-to-user privileged -z openshell -n openshell
oc adm policy add-scc-to-user privileged -z openshell-sandbox -n openshell
```
Or re-run `make deploy` which applies these automatically when `oc` is present.

**Sandbox stuck in `Waiting` / never becomes Ready (image pull)**
→ The sandbox image is being pulled (~1.4 GB). First pull takes 2–5 minutes. Watch progress with: `kubectl describe pod <name> -n openshell`

**mTLS error: `certificate signed by unknown authority`**
→ The CA cert in `auth/openshell/ca.crt` doesn't match the cluster. Re-run `make deploy` to re-extract certs, or manually:
```sh
kubectl -n openshell get secret openshell-client-tls \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > auth/openshell/ca.crt
kubectl -n openshell get secret openshell-client-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > auth/openshell/tls.crt
kubectl -n openshell get secret openshell-client-tls \
  -o jsonpath='{.data.tls\.key}' | base64 -d > auth/openshell/tls.key
```
Then re-run `make openshell-register` to copy the updated certs to the gateway config dir.

**`kubectl` context wrong (deploying to production instead of kind)**
→ Run `kind export kubeconfig --name swarmer` before any `make` targets to point kubectl at the kind cluster.

**Session stays in `pending` / never reaches `running`**
→ Check Swarmer logs for errors from `_run_openshell_agent`. The sandbox image must be pullable from within the cluster node — for kind, load it with `kind load docker-image <image> --name swarmer`.
