# Install Agent Swarm on OpenShift

This guide deploys the **swarmer** dashboard to an OpenShift cluster using OpenShift OAuth for authentication. Execute each step in order using the `oc` CLI.

## Prerequisites

- `oc` CLI installed and logged in to the target cluster
- `cluster-admin` privileges (required for OAuthClient and ClusterRole creation)

---

## Step 1 — Verify cluster connection

```bash
oc whoami
oc cluster-info
```

Expected: your username and the cluster API URL. If this fails, run `oc login` first.

---

## Step 2 — Determine cluster-specific values

```bash
APPS_DOMAIN=$(oc get ingress.config cluster -o jsonpath='{.spec.domain}')
SWARMER_HOST="swarmer.${APPS_DOMAIN}"
OAUTH_HOST=$(oc get route oauth-openshift -n openshift-authentication -o jsonpath='{.spec.host}')
OPENSHIFT_OAUTH_URL="https://${OAUTH_HOST}"
SWARMER_IMAGE="quay.io/jpacker/swarmer:$(cat VERSION)"

# Agent tool images — update these to match your registry
AGENT_IMAGE_OPENCODE="quay.io/jpacker/opencode:0.1.1"
AGENT_IMAGE_CRUSH="ghcr.io/gurnben/crush-container:latest"

echo "App domain:   ${APPS_DOMAIN}"
echo "Swarmer URL:  https://${SWARMER_HOST}"
echo "OAuth URL:    ${OPENSHIFT_OAUTH_URL}"
echo "Image:        ${SWARMER_IMAGE}"
echo "OpenCode img: ${AGENT_IMAGE_OPENCODE}"
echo "Crush img:    ${AGENT_IMAGE_CRUSH}"
```

Verify the output looks correct before continuing.

---

## Step 3 — Apply shared resources (namespace, RBAC, PVC)

```bash
oc apply -f k8s/swarmer/namespace.yaml
oc apply -f k8s/swarmer/rbac.yaml
oc apply -f k8s/swarmer/pvc.yaml
```

---

## Step 4 — Create the swarmer secret

```bash
SWARMER_SECRET_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
oc create secret generic swarmer-secret \
  --from-literal=SWARMER_SECRET_KEY="${SWARMER_SECRET_KEY}" \
  -n swarmer --dry-run=client -o yaml | oc apply -f -
```

> **Note:** Each run regenerates the key, invalidating existing sessions. Skip this step on re-deploys if you want to preserve sessions.

---

## Step 5 — Apply OpenShift service

```bash
oc apply -f k8s/openshift/service.yaml
```

---

## Step 6 — Apply Route

```bash
sed "s|SWARMER_HOST|${SWARMER_HOST}|g" k8s/openshift/route.yaml | oc apply -f -
```

---

## Step 7 — Apply OAuthClient

This requires `cluster-admin`.

```bash
sed "s|SWARMER_HOST|${SWARMER_HOST}|g" k8s/openshift/oauth-client.yaml | oc apply -f -
```

---

## Step 8 — Apply Deployment

```bash
sed -e "s|SWARMER_IMAGE|${SWARMER_IMAGE}|g" \
    -e "s|OPENSHIFT_OAUTH_URL_VALUE|${OPENSHIFT_OAUTH_URL}|g" \
    -e "s|AGENT_IMAGE_OPENCODE_VALUE|${AGENT_IMAGE_OPENCODE}|g" \
    -e "s|AGENT_IMAGE_CRUSH_VALUE|${AGENT_IMAGE_CRUSH}|g" \
    k8s/openshift/deployment.yaml | oc apply -f -
```

---

## Step 9 — Wait for rollout

```bash
oc rollout status deployment/swarmer -n swarmer --timeout=120s
```

---

## Step 10 — Verify and access

```bash
oc get pods -n swarmer
oc get route swarmer -n swarmer
echo "Swarmer is available at: https://${SWARMER_HOST}"
```

Open `https://${SWARMER_HOST}` in a browser. You will be redirected to OpenShift OAuth login.

---

## Teardown

```bash
oc delete -f k8s/openshift/deployment.yaml --ignore-not-found
oc delete -f k8s/openshift/service.yaml --ignore-not-found
oc delete route swarmer -n swarmer --ignore-not-found
oc delete oauthclient swarmer --ignore-not-found
oc delete -f k8s/swarmer/pvc.yaml --ignore-not-found
oc delete secret swarmer-secret -n swarmer --ignore-not-found
oc delete -f k8s/swarmer/rbac.yaml --ignore-not-found
oc delete -f k8s/swarmer/namespace.yaml --ignore-not-found
```
