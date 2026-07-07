# Kustomize Deployment

An alternative to the Makefile-based deployment documented in the main [README](https://github.com/stolostron/agent-swarm#running). Uses Kustomize overlays instead of `make k8s-deploy` / `make openshift-deploy` for declarative, repeatable deployments.

## Two deployment flavors

### cluster-admin — full multi-namespace deployment

Matches the upstream `make openshift-deploy` flow. Requires **cluster-admin** privileges.

- Creates its own `swarmer` namespace
- ClusterRole + ClusterRoleBinding for cross-namespace workspace management
- OAuthClient for "Sign in with OpenShift" button
- `swarmer-user` ClusterRole for workspace access grants (`make grant-workspace-access`)
- `swarmer-workspace-creator` ClusterRole for workspace creation grants (`make grant-workspace-create`)
- Each workspace gets its own Kubernetes namespace

Best for: dedicated clusters, production deployments, multi-tenant setups.

### namespace-scoped — single-namespace deployment

Deploys into an **existing** namespace with no cluster-level permissions required.

- Namespace-scoped Role + RoleBinding only
- All workspaces share the target namespace (`K8S_NAMESPACE`)
- Bearer token login only (no OAuthClient — requires cluster-admin to create)
- No namespace creation/deletion (the namespace must already exist)

Best for: shared clusters, ephemeral environments, CI/CD, environments without cluster-admin.

### Comparison

| | cluster-admin | namespace-scoped |
|---|---|---|
| **Permissions** | cluster-admin | namespace editor |
| **Namespace** | Creates `swarmer` | Uses existing namespace |
| **RBAC** | ClusterRole / ClusterRoleBinding | Role / RoleBinding |
| **Workspace isolation** | One namespace per workspace | All workspaces share one namespace |
| **Auth** | OpenShift OAuth + bearer token | Bearer token only |
| **OAuthClient** | Included | Not included |
| **User management** | `make user-token` / `make grant-workspace-access` / `make grant-workspace-create` | Use your existing cluster credentials |

## Prerequisites

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

## Deploying with cluster-admin

```sh
# 1. Create the secret key
oc create secret generic swarmer-secret \
  --from-literal=SWARMER_SECRET_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())") \
  -n swarmer --dry-run=client -o yaml | oc apply -f -

# 2. Deploy (set image via overlay or kustomize edit)
oc apply -k kustomize/base/cluster-admin

# 3. Set the image (replace SWARMER_IMAGE placeholder)
oc set image deployment/swarmer swarmer=<your-image> -n swarmer

# 4. Set agent image and OAuth URL
oc set env deployment/swarmer -n swarmer \
  AGENT_IMAGE_OPENCODE=ghcr.io/anomalyco/opencode:latest \
  OPENSHIFT_OAUTH_URL=https://$(oc get route oauth-openshift -n openshift-authentication -o jsonpath='{.spec.host}')

# 5. Update OAuthClient redirect URI
SWARMER_HOST=$(oc get route swarmer -n swarmer -o jsonpath='{.spec.host}')
oc patch oauthclient swarmer --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/redirectURIs/0\",\"value\":\"https://${SWARMER_HOST}/auth/callback\"}]"
```

Dashboard: `https://<route-host>`

### User onboarding

```sh
make user-token SA_USER=alice                                  # create user + print token
make grant-workspace-access SA_USER=alice WORKSPACE_NS=team-a  # grant workspace access
make grant-workspace-create SA_USER=alice                      # allow self-service workspace creation (optional)
```

## Deploying with namespace-scoped (no cluster-admin)

### Quick start

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
#    - IMAGE_REGISTRY → your registry (e.g. image-registry.openshift-image-registry.svc:5000)
#    - Agent image values if different from defaults

# 4. Deploy
oc apply -k kustomize/overlays/my-env
```

Dashboard: `https://swarmer-<namespace>.apps.<cluster-domain>`

### Using the example overlay

The `overlays/ephemeral/` directory is a template with three placeholders:

| Placeholder | Replace with | Example |
|---|---|---|
| `NAMESPACE` | Target namespace name | `ephemeral-abc123` |
| `IMAGE_REGISTRY/NAMESPACE/swarmer` | Full image reference (without tag) | `image-registry.openshift-image-registry.svc:5000/ephemeral-abc123/swarmer` |

The agent image defaults to `ghcr.io/anomalyco/opencode:latest`.

## Teardown

```sh
# cluster-admin
oc delete -k kustomize/base/cluster-admin

# namespace-scoped
oc delete -k kustomize/overlays/my-env
```

## Differences from Makefile deployment

The Kustomize deployment is functionally equivalent to `make openshift-deploy` / `make k8s-deploy`. Key differences:

- **Declarative** — all configuration is in YAML files, not shell variable substitution
- **No Makefile required** — deploy with `oc apply -k` alone
- **Overlay pattern** — environment-specific values (namespace, image, env vars) are separated from the base manifests
- **User onboarding** — `make user-token`, `make grant-workspace-access`, and `make grant-workspace-create` still work alongside Kustomize deployments (they operate on the running cluster, not the manifests)
