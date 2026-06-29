---
name: openshell-deploy
description: Use when installing, configuring, or redeploying OpenShell for the Swarmer project — covers both local kind clusters and OpenShift clusters, SCC fixes, cert extraction and validation, secret creation, and wiring the deployment manifest.
---

# OpenShell Deploy Skill

Covers end-to-end OpenShell setup for Swarmer — from Helm install to a running
pod with correct mTLS certs. Handles both local kind and OpenShift targets.

OpenShell is installed and managed automatically by `make deploy` and `make delete`.
The targets below exist for manual diagnostics and cert rotation.

## Key Makefile targets

| Target | What it does |
|---|---|
| `make deploy` | Deploy swarmer + install OpenShell if not present (auto-detects OpenShift vs generic K8s) |
| `make delete` | Remove swarmer and uninstall OpenShell from the cluster |
| `make status` | Show Helm release, gateway pods, CRDs, and local cert files |
| `make connect-openshell` | Port-forward the OpenShell gateway gRPC port to the active CLI gateway port |
| `make kind-deploy` | One-shot local dev: create kind cluster + build + deploy (includes OpenShell) |

## Workflow: fresh OpenShift install

1. **Verify context**
   ```sh
   kubectl config current-context
   ```
   Must point at the target OpenShift cluster, not a local kind cluster.

2. **Deploy**
   ```sh
   make deploy
   ```
   Auto-detects OpenShift (via `route.openshift.io` API). Installs OpenShell if
   not present, grants `anyuid` SCC, waits for gateway readiness, extracts certs
   to `auth/openshell/`, applies ClusterIP service + Route + OAuthClient, and
   deploys Swarmer.

3. **Verify**
   ```sh
   make status
   kubectl rollout status deployment/swarmer -n swarmer
   ```

## Workflow: cert rotation / stale certs

If `auth/openshell/` certs are from a different cluster or old deploy, re-extract
them manually and update the in-cluster secret:

```sh
# Re-extract from cluster
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.ca\.crt}'  | base64 -d > auth/openshell/ca.crt
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > auth/openshell/tls.crt
kubectl -n openshell get secret openshell-client-tls -o jsonpath='{.data.tls\.key}' | base64 -d > auth/openshell/tls.key

# Update the in-cluster secret
kubectl create secret generic openshell-tls \
  --from-file=ca.crt=auth/openshell/ca.crt \
  --from-file=tls.crt=auth/openshell/tls.crt \
  --from-file=tls.key=auth/openshell/tls.key \
  -n swarmer --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/swarmer -n swarmer
kubectl rollout status deployment/swarmer -n swarmer --timeout=120s
```

Always fingerprint-check before and after:

```sh
# Cluster fingerprint
kubectl get secret openshell-client-tls -n openshell \
  -o jsonpath='{.data.ca\.crt}' | base64 -d | openssl x509 -noout -fingerprint

# Local file fingerprint
openssl x509 -noout -fingerprint -in auth/openshell/ca.crt
```

## Workflow: local kind cluster

For local development, the gateway is not reachable in-cluster — use a
port-forward instead:

```sh
# Terminal 1 — keep running
make connect-openshell

# .env values for local dev
OPENSHELL_GATEWAY_URL=localhost:17670
OPENSHELL_TLS_CERT=auth/openshell/tls.crt
OPENSHELL_TLS_KEY=auth/openshell/tls.key
OPENSHELL_TLS_CA=auth/openshell/ca.crt
OPENSHELL_BEARER_TOKEN=
```

Run `python3 scripts/openshell_smoke_test.py` to verify before testing through
the UI.

## Common failures on OpenShift

| Symptom | Cause | Fix |
|---|---|---|
| `pods "openshell-0" is forbidden: unable to validate against any security context constraint` | SCC mismatch — UID 1000 not allowed | `oc adm policy add-scc-to-user anyuid -z openshell -n openshell` (done automatically by `make deploy`) |
| Helm status `pending-install` with no pods | SCC blocked StatefulSet creation | Fix SCC, then verify with `kubectl rollout status statefulset/openshell -n openshell` |
| gRPC `certificate signed by unknown authority` | Stale certs from previous cluster | Re-extract and fingerprint-check (see cert rotation above) |
| `openshell-tls` secret not found in `swarmer` | Secret not created in app namespace | Re-run `make deploy` |
| Swarmer pod crashloops after cert update | Old secret still mounted | `kubectl rollout restart deployment/swarmer -n swarmer` |
