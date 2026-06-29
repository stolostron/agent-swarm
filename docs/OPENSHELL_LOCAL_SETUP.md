# OpenShell Local Development Setup

Step-by-step guide to running Swarmer with a live OpenShell sandbox backend on a local kind cluster.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| `kind` | any | `kind version` |
| `kubectl` | any | must match cluster |
| `helm` | 3.8+ | required for OCI chart support |
| Python | 3.12 | `python3 --version` |
| `openshell` pip pkg | 0.0.0a0+ | `pip install openshell` |

> **Note**: The `openshell` Python package must match the gateway version. Run `pip show openshell` and confirm the installed version matches `OPENSHELL_VERSION` in the Makefile.

## Quick Start

### 1. Deploy Swarmer + OpenShell into kind

```sh
make setup-secret   # only needed once — generates auth/secret.key
make kind-deploy
```

`make kind-deploy` creates the kind cluster (idempotent), builds and loads the swarmer image, then runs `make deploy` which auto-detects OpenShell and installs it if not present:
1. Installs the Kubernetes Agent Sandbox CRDs at `AGENT_SANDBOX_VERSION` (pinned to v0.4.6 — **do not upgrade to v0.5.0+** until the OpenShell gateway supports v1beta1 ownerReferences; see gotchas below)
2. Runs `helm upgrade --install` from `oci://ghcr.io/nvidia/openshell/helm-chart` (version from `OPENSHELL_VERSION` in Makefile) into the `openshell` namespace
3. Waits for the gateway pod to be ready
4. Extracts mTLS client certs to `auth/openshell/`
5. Deploys Swarmer to the cluster

Verify with:

```sh
make status
```

### 3. Port-forward the OpenShell gateway

In a separate terminal (keep it running):

```sh
make connect-openshell
```

Or manually:

```sh
kubectl port-forward -n openshell svc/openshell 17670:8080
```

### 4. Configure `.env`

Add these to your `.env`:

```sh
OPENSHELL_GATEWAY_URL=localhost:17670
OPENSHELL_TLS_CERT=auth/openshell/tls.crt
OPENSHELL_TLS_KEY=auth/openshell/tls.key
OPENSHELL_TLS_CA=auth/openshell/ca.crt
OPENSHELL_BEARER_TOKEN=
```

Copy from `.env.example` if you don't have a `.env` yet:

```sh
cp .env.example .env
# then set the four lines above (leave BEARER_TOKEN empty for local dev)
```

> **Auth model**: The local kind gateway uses `allowUnauthenticatedUsers=true` — mTLS client certs are the only auth mechanism. No bearer token is needed. `OPENSHELL_BEARER_TOKEN` should be left blank.

### 5. Start Swarmer dev server

```sh
make dev            # pip install + uvicorn at http://localhost:8090
```

### 6. Smoke test via SDK (recommended before testing through UI)

```sh
python3 scripts/openshell_smoke_test.py
```

Expected output:

```text
Connecting to localhost:17670 ...
Creating sandbox ...
Created sandbox: <name>
Waiting for ready (first run may take ~3 min for image pull) ...
Running 'echo hello' ...
Exec result: ExecResult(exit_code=0, stdout='hello\n', stderr='')
Deleting sandbox ...
Deleted: True
OK
```

### 7. Launch a session through the UI

1. Open `http://localhost:8090` and log in (use `make user-token SA_USER=<name>` for a token)
2. Create a workspace
3. Add a session and click **Launch**
4. The session should enter `running` phase — Swarmer will create an OpenShell sandbox instead of a K8s pod
5. Confirm: `kubectl get sandboxes -n openshell`

## One-shot kind deploy (alternative)

For a fully automated local dev setup from scratch:

```sh
make setup-secret   # only needed once
make kind-deploy    # create cluster + build image + deploy (includes OpenShell)
make connect        # port-forward dashboard to localhost:8080
```

## Teardown

```sh
# Stop swarmer dev server (Ctrl-C in the dev terminal)
# Stop the port-forward (Ctrl-C in that terminal)
make delete         # uninstall swarmer + OpenShell from cluster
make kind-delete    # delete the kind cluster entirely
```

## Troubleshooting

**`helm upgrade` fails with "OCI registry not found"**
→ Ensure you can reach `ghcr.io`. Run `helm pull oci://ghcr.io/nvidia/openshell/helm-chart --version $(OPENSHELL_VERSION)` to test auth.

**gRPC `Connection refused` on `localhost:17670`**
→ The port-forward is not running. Start it with `make connect-openshell` in a separate terminal.

**gRPC `UNAUTHENTICATED: missing authorization header`**
→ The gateway was installed without `allowUnauthenticatedUsers=true`. Re-run `make deploy` (it's idempotent).

**gRPC `UNAUTHENTICATED: invalid token: JSON error: missing field 'sandbox_id'`**
→ A bearer token is set in `.env` that the gateway is trying to validate as a sandbox session token. Clear `OPENSHELL_BEARER_TOKEN=` in `.env`.

**Sandbox stuck in `Waiting` / never becomes Ready**
→ The sandbox image (`ghcr.io/nvidia/openshell-community/sandboxes/base:latest`) is being pulled — it's ~1.4 GB. The first pull takes 2–5 minutes. Use `kubectl describe pod <name> -n openshell` to watch progress.

**mTLS error: `certificate signed by unknown authority`**
→ The CA cert in `auth/openshell/ca.crt` doesn't match the cluster. Re-run `make deploy` to re-extract certs, or manually:
```sh
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > auth/openshell/ca.crt
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > auth/openshell/tls.crt
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.tls\.key}' | base64 -d > auth/openshell/tls.key
kubectl create secret generic openshell-tls \
  --from-file=ca.crt=auth/openshell/ca.crt \
  --from-file=tls.crt=auth/openshell/tls.crt \
  --from-file=tls.key=auth/openshell/tls.key \
  -n swarmer --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/swarmer -n swarmer
```

**`kubectl` context wrong (deploying to production instead of kind)**
→ Run `kind export kubeconfig --name swarmer` before running any `make` targets to ensure kubectl is pointed at the kind cluster.

**Session stays in `pending` / never reaches `running`**
→ Check `_run_openshell_agent` logs in the Swarmer console. The sandbox image must be pullable from within the kind node — load it with `kind load docker-image <image> --name swarmer` if needed.
