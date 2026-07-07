# ──────────────────────────────────────────────────────────────
#  Swarmer — Makefile
# ──────────────────────────────────────────────────────────────
#  Variables (override on the command line or in .env)
# ──────────────────────────────────────────────────────────────
-include .env

# Container image settings
IMAGE        ?= swarmer
IMAGE_TAG    ?= $(shell cat VERSION)
REGISTRY     ?=
# If REGISTRY is set, full ref is REGISTRY/IMAGE:TAG, otherwise IMAGE:TAG
IMAGE_REF     = $(if $(REGISTRY),$(REGISTRY)/$(IMAGE):$(IMAGE_TAG),$(IMAGE):$(IMAGE_TAG))

# docker or podman
CONTAINER_CMD ?= podman

# Agent tool images (overridable via .env or command line)
AGENT_IMAGE_OPENCODE ?=
AGENT_IMAGE_CRUSH    ?=

# Kubernetes
NAMESPACE            ?= swarmer
KIND_CLUSTER         ?= swarmer
OPENSHIFT_OAUTH_URL  ?=
SWARMER_HOST         ?=

# Port-forward ports
LOCAL_PORT      ?= 8080
OS_LOCAL_PORT   ?= 17671

# User token duration
# agent-containers build defaults (registry + image tag — checked in)
AC_DEFAULTS ?= .push-defaults

# OpenShell gateway
OPENSHELL_VERSION        ?= 0.0.70
# agent-sandbox v0.4.6 is required — v0.5.0+ graduates the CRD to v1beta1 and
# sets ownerReference apiVersion=agents.x-k8s.io/v1beta1 on sandbox pods, but
# the OpenShell gateway (through at least 0.0.70) checks for v1alpha1 in
# IssueSandboxToken, causing "Policy fetch failed" on every sandbox launch.
AGENT_SANDBOX_VERSION    ?= v0.4.6
OPENSHELL_NAMESPACE      ?= openshell
OPENSHELL_TLS_DIR        ?= auth/openshell

# ──────────────────────────────────────────────────────────────
#  Phony targets
# ──────────────────────────────────────────────────────────────
.PHONY: setup-secret user-token grant-workspace grant-workspace-access grant-workspace-create \
        dev lint test smoke-test-jira \
        sync-images image-build image-push \
        deploy delete connect openshell-register connect-openshell status \
        kind-deploy kind-delete \
        help

# ──────────────────────────────────────────────────────────────
#  Developer tooling
# ──────────────────────────────────────────────────────────────

sync-images:  ## Sync AGENT_IMAGE_OPENCODE / AGENT_IMAGE_CRUSH in .env from .push-defaults
	@test -f $(AC_DEFAULTS) || (echo "$(AC_DEFAULTS) not found — create/update .push-defaults first" && exit 1)
	$(eval AC_REGISTRY := $(shell grep '^REGISTRY=' $(AC_DEFAULTS) | cut -d= -f2-))
	$(eval AC_TAG      := $(shell grep '^IMAGE_TAG=' $(AC_DEFAULTS) | cut -d= -f2-))
	@echo "Syncing agent images → $(AC_REGISTRY)/{opencode,crush}:$(AC_TAG)"
	@sed -i "s|^AGENT_IMAGE_OPENCODE=.*|AGENT_IMAGE_OPENCODE=$(AC_REGISTRY)/opencode:$(AC_TAG)|" .env
	@sed -i "s|^AGENT_IMAGE_CRUSH=.*|AGENT_IMAGE_CRUSH=$(AC_REGISTRY)/crush:$(AC_TAG)|" .env
	@echo "Updated .env"

setup-secret:  ## Generate a new SWARMER_SECRET_KEY and save to auth/secret.key
	@mkdir -p auth
	@python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > auth/secret.key
	@echo "Secret key written to auth/secret.key"

user-token:  ## Issue a login token for a K8s user  (SA_USER=alice, TOKEN_DURATION=8h)
	@test -n "$(SA_USER)" || (echo "Usage: make user-token SA_USER=<name>" && exit 1)
	@kubectl create serviceaccount $(SA_USER) -n $(NAMESPACE) \
	  --dry-run=client -o yaml | kubectl apply -f - > /dev/null
	@echo ""
	@echo "Token for '$(SA_USER)' (valid $(TOKEN_DURATION)):"
	@echo "──────────────────────────────────────────────────"
	@kubectl create token $(SA_USER) -n $(NAMESPACE) --duration=$(TOKEN_DURATION)
	@echo "──────────────────────────────────────────────────"
	@echo "Paste this token into the Swarmer login page."
	@echo "Grant workspace access with: make grant-workspace-access SA_USER=$(SA_USER) WORKSPACE_NS=<ns>"

grant-workspace-access:  ## Grant a user access to a specific workspace namespace  (SA_USER=alice, WORKSPACE_NS=my-project)
	@test -n "$(SA_USER)"      || (echo "Usage: make grant-workspace-access SA_USER=<name> WORKSPACE_NS=<ns>" && exit 1)
	@test -n "$(WORKSPACE_NS)" || (echo "Usage: make grant-workspace-access SA_USER=<name> WORKSPACE_NS=<ns>" && exit 1)
	kubectl create rolebinding swarmer-user-$(SA_USER) \
	  --clusterrole=swarmer-user \
	  --serviceaccount=$(NAMESPACE):$(SA_USER) \
	  --namespace=$(WORKSPACE_NS) \
	  --dry-run=client -o yaml | kubectl apply -f -
	@echo "$(SA_USER) can now access workspace namespace '$(WORKSPACE_NS)'."

grant-workspace-create:  ## Allow a user to create new workspaces  (SA_USER=alice)
	@test -n "$(SA_USER)" || (echo "Usage: make grant-workspace-create SA_USER=<name>" && exit 1)
	kubectl create clusterrolebinding swarmer-workspace-creator-$(SA_USER) \
	  --clusterrole=swarmer-workspace-creator \
	  --serviceaccount=$(NAMESPACE):$(SA_USER) \
	  --dry-run=client -o yaml | kubectl apply -f -
	@echo "$(SA_USER) can now create new workspaces (but cannot see others' workspaces without grant-workspace-access)."

grant-workspace: grant-workspace-access  ## Deprecated alias for grant-workspace-access

dev:  ## Install deps and run development server with auto-reload (uses local kubeconfig)
	pip install -r requirements.txt
	@echo ""
	@echo "╔══════════════════════════════════════════════════════╗"
	@echo "║  Swarmer dev server → http://localhost:8090          ║"
	@echo "╚══════════════════════════════════════════════════════╝"
	@echo ""
	K8S_IN_CLUSTER=false uvicorn swarmer.main:app --host 0.0.0.0 --port 8090 --reload

lint:  ## Run ruff linter
	ruff check swarmer/

test:  ## Run unit tests (excludes Playwright browser tests)
	python3 -m pytest tests/ -q --ignore=tests/test_ui_patternfly.py
	python3 -m pip install -q -e "mcp-server[dev]"
	python3 -m pytest mcp-server/tests/ -q --rootdir=mcp-server

smoke-test-jira:  ## Run Jira MCP OpenShell e2e smoke test (requires running OpenShell gateway)
	python3 scripts/openshell_jira_smoke_test.py

# ──────────────────────────────────────────────────────────────
#  Container image
# ──────────────────────────────────────────────────────────────

image-build: sync-images  ## Build the swarmer container image  (REGISTRY, SILENT=1 to skip version prompt)
	@set -e; \
	CURRENT=$$(cat VERSION); \
	if [ "$(SILENT)" != "1" ]; then \
		printf "Image version [$$CURRENT]: "; \
		read INPUT; \
		if [ -n "$$INPUT" ]; then \
			printf "$$INPUT\n" > VERSION; \
			TAG=$$INPUT; \
		else \
			TAG=$$CURRENT; \
		fi; \
	else \
		TAG=$$CURRENT; \
	fi; \
	IMAGE_REF="$(if $(REGISTRY),$(REGISTRY)/$(IMAGE),$(IMAGE)):$$TAG"; \
	echo "Building $$IMAGE_REF..."; \
	$(CONTAINER_CMD) build -f Containerfile -t "$$IMAGE_REF" .; \
	echo "Built: $$IMAGE_REF"

image-push:  ## Push image to registry  (requires REGISTRY=..., uses VERSION file)
	@test -n "$(REGISTRY)" || (echo "Set REGISTRY=your.registry.example.com" && exit 1)
	@TAG=$$(cat VERSION); \
	IMAGE_REF="$(REGISTRY)/$(IMAGE):$$TAG"; \
	echo "Pushing $$IMAGE_REF..."; \
	$(CONTAINER_CMD) push "$$IMAGE_REF"; \
	echo "Pushed: $$IMAGE_REF"

# ──────────────────────────────────────────────────────────────
#  Deploy / Delete  (auto-detects OpenShift vs generic K8s)
# ──────────────────────────────────────────────────────────────

deploy:  ## Deploy swarmer to the current kubectl context  (SILENT=1 for non-interactive)
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first." && exit 1)
	@echo "Deploying $(IMAGE_REF) → namespace $(NAMESPACE)..."
	@# ── 1. Namespace + RBAC + PVC ──────────────────────────────────────────
	kubectl apply -f k8s/swarmer/namespace.yaml
	kubectl apply -f k8s/swarmer/rbac.yaml
	kubectl apply -f k8s/swarmer/pvc.yaml
	@# ── 2. OpenShell (install if not already present) ──────────────────────
	@set -e; \
	HELM_VER=$$(helm version --short 2>/dev/null | grep -oP 'v\K[0-9]+\.[0-9]+' | head -1); \
	HELM_MAJOR=$$(echo "$$HELM_VER" | cut -d. -f1); \
	HELM_MINOR=$$(echo "$$HELM_VER" | cut -d. -f2); \
	if [ -z "$$HELM_VER" ] || { [ "$$HELM_MAJOR" -lt 4 ] && { [ "$$HELM_MAJOR" -lt 3 ] || [ "$$HELM_MINOR" -lt 8 ]; }; }; then \
	  echo "Error: Helm 3.8+ required for OCI chart support (found: $$(helm version --short 2>/dev/null || echo 'not installed'))"; \
	  exit 1; \
	fi; \
	if ! helm status openshell -n $(OPENSHELL_NAMESPACE) > /dev/null 2>&1; then \
	  echo "OpenShell not found — installing $(OPENSHELL_VERSION)..."; \
	  kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$(AGENT_SANDBOX_VERSION)/manifest.yaml; \
	  kubectl create namespace $(OPENSHELL_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -; \
	  DOCKER_CONFIG=$$(mktemp -d) helm upgrade --install openshell \
	    oci://ghcr.io/nvidia/openshell/helm-chart \
	    --version $(OPENSHELL_VERSION) \
	    --namespace $(OPENSHELL_NAMESPACE) \
	    --set server.auth.allowUnauthenticatedUsers=true \
	    --wait --timeout 5m; \
	  echo "✓ OpenShell $(OPENSHELL_VERSION) installed."; \
	else \
	  echo "OpenShell already installed."; \
	fi; \
	# Grant OpenShift SCCs required for sandbox pods (no-op on plain k8s / if oc is absent) \
	if command -v oc > /dev/null 2>&1; then \
	  oc adm policy add-scc-to-user anyuid    -z openshell         -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true; \
	  oc adm policy add-scc-to-user anyuid    -z openshell-sandbox -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true; \
	  oc adm policy add-scc-to-user privileged -z openshell         -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true; \
	  oc adm policy add-scc-to-user privileged -z openshell-sandbox -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true; \
	  echo "  ✓ OpenShift SCC grants applied (anyuid + privileged for openshell and openshell-sandbox)"; \
	fi
	@# ── 3. Extract OpenShell mTLS certs ────────────────────────────────────
	@mkdir -p $(OPENSHELL_TLS_DIR)
	@kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.ca\.crt}'  | base64 -d > $(OPENSHELL_TLS_DIR)/ca.crt
	@kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.tls\.crt}' | base64 -d > $(OPENSHELL_TLS_DIR)/tls.crt
	@kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.tls\.key}' | base64 -d > $(OPENSHELL_TLS_DIR)/tls.key
	@echo "✓ mTLS certs written to $(OPENSHELL_TLS_DIR)/"
	@# Register / refresh this cluster's OpenShell gateway in the local CLI
	$(MAKE) openshell-register
	@# ── 4. Secrets ─────────────────────────────────────────────────────────
	@# swarmer-secret
	@test -f auth/secret.key
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic swarmer-secret \
	  --from-literal=SWARMER_SECRET_KEY=$$(cat auth/secret.key) \
	  -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@# openshell-tls secret
	@test -f $(OPENSHELL_TLS_DIR)/ca.crt || (echo "OpenShell mTLS certs not found. Run 'make deploy' to install OpenShell first." && exit 1)
	kubectl create secret generic openshell-tls \
	  --from-file=ca.crt=$(OPENSHELL_TLS_DIR)/ca.crt \
	  --from-file=tls.crt=$(OPENSHELL_TLS_DIR)/tls.crt \
	  --from-file=tls.key=$(OPENSHELL_TLS_DIR)/tls.key \
	  -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@# Generate / refresh OpenShell bearer token
	@TOKEN=$$(python3 scripts/openshell_gen_token.py 2>/dev/null || true); \
	if [ -n "$$TOKEN" ]; then \
	  sed -i '/^OPENSHELL_BEARER_TOKEN=/d' .env 2>/dev/null || true; \
	  echo "OPENSHELL_BEARER_TOKEN=$$TOKEN" >> .env; \
	  echo "✓ OPENSHELL_BEARER_TOKEN refreshed in .env (valid 30 days)"; \
	fi
	@# ── 5. Detect OAuth URL ────────────────────────────────────────────────
	@set -e; \
	OAUTH_URL="$(OPENSHIFT_OAUTH_URL)"; \
	if [ -z "$$OAUTH_URL" ]; then \
	  DETECTED=$$(kubectl get route oauth-openshift -n openshift-authentication \
	    -o jsonpath='{.spec.host}' 2>/dev/null || true); \
	  if [ -n "$$DETECTED" ]; then \
	    OAUTH_URL="https://$$DETECTED"; \
	    echo "Auto-detected OpenShift OAuth URL: $$OAUTH_URL"; \
	  else \
	    if [ "$(SILENT)" != "1" ]; then \
	      printf "OPENSHIFT_OAUTH_URL (leave blank for token-only login): "; \
	      read OAUTH_URL; \
	    fi; \
	  fi; \
	fi; \
	\
	PREV_MAX=$$(grep '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null | cut -d= -f2 || true); \
	DEF_MAX=$${PREV_MAX:-5}; \
	MAX_VAL="$(MAX_CONCURRENT_AGENTS)"; \
	if [ -z "$$MAX_VAL" ] && [ "$(SILENT)" != "1" ]; then \
	  printf "MAX_CONCURRENT_AGENTS [$$DEF_MAX]: "; \
	  read MAX_INPUT; \
	  MAX_VAL=$${MAX_INPUT:-$$DEF_MAX}; \
	else \
	  MAX_VAL=$${MAX_VAL:-$$DEF_MAX}; \
	fi; \
	grep -v '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null > .deploy-defaults.tmp || true; \
	echo "MAX_CONCURRENT_AGENTS=$$MAX_VAL" >> .deploy-defaults.tmp; \
	mv .deploy-defaults.tmp .deploy-defaults; \
	\
	OPENSHELL_GW=$$(kubectl get svc openshell -n $(OPENSHELL_NAMESPACE) \
	  -o jsonpath='{.metadata.name}.{.metadata.namespace}.svc.cluster.local:{.spec.ports[?(@.appProtocol=="grpc")].port}' \
	  2>/dev/null || true); \
	if [ -z "$$OPENSHELL_GW" ]; then \
	  OPENSHELL_GW="openshell.$(OPENSHELL_NAMESPACE).svc.cluster.local:8080"; \
	fi; \
	\
	DEFAULT_TOOL="opencode"; \
	\
	IS_OCP=$$(kubectl api-resources --api-group=route.openshift.io 2>/dev/null | grep -c routes || true); \
	if [ "$$IS_OCP" -gt 0 ]; then \
	  echo "OpenShift cluster detected — applying OCP resources..."; \
	  kubectl apply -f k8s/openshift/service.yaml; \
	  if [ -n "$(SWARMER_HOST)" ]; then \
	    kubectl apply -f k8s/openshift/route.yaml; \
	    kubectl patch route swarmer -n $(NAMESPACE) --type=merge \
	      -p "{\"spec\":{\"host\":\"$(SWARMER_HOST)\"}}"; \
	  else \
	    kubectl apply -f k8s/openshift/route.yaml; \
	  fi; \
	  ROUTE_HOST="$(SWARMER_HOST)"; \
	  if [ -z "$$ROUTE_HOST" ]; then \
	    for i in $$(seq 1 15); do \
	      ROUTE_HOST=$$(kubectl get route swarmer -n $(NAMESPACE) \
	        -o jsonpath='{.spec.host}' 2>/dev/null); \
	      [ -n "$$ROUTE_HOST" ] && break; \
	      sleep 2; \
	    done; \
	  fi; \
	  if [ -z "$$ROUTE_HOST" ]; then \
	    echo "Error: Route hostname not assigned after 30s."; \
	    echo "       Check: kubectl get route swarmer -n $(NAMESPACE)"; \
	    exit 1; \
	  fi; \
	  sed "s|SWARMER_HOST|$$ROUTE_HOST|g" k8s/openshift/oauth-client.yaml | kubectl apply -f -; \
	  echo "OAuthClient registered → https://$$ROUTE_HOST/auth/callback"; \
	  PULL_POLICY="Always"; \
	  FS_GROUP="1001"; \
	  REDIRECT_URL=""; \
	else \
	  echo "Generic Kubernetes cluster — applying standard resources..."; \
	  kubectl apply -f k8s/swarmer/service.yaml; \
	  ROUTE_HOST=""; \
	  PULL_POLICY="IfNotPresent"; \
	  FS_GROUP=""; \
	  REDIRECT_URL=""; \
	fi; \
	\
	sed "s|SWARMER_IMAGE|$(IMAGE_REF)|g; \
	     s|IMAGE_PULL_POLICY_VALUE|$$PULL_POLICY|g; \
	     /fsGroup: FS_GROUP_VALUE/{ s|FS_GROUP_VALUE|$$FS_GROUP|; /fsGroup: $$/d; }; \
	     s|OPENSHIFT_OAUTH_URL_VALUE|$$OAUTH_URL|g; \
	     s|REDIRECT_BASE_URL_VALUE|$$REDIRECT_URL|g; \
	     s|DEFAULT_AGENT_TOOL_VALUE|$$DEFAULT_TOOL|g; \
	     s|AGENT_IMAGE_OPENCODE_VALUE|$(AGENT_IMAGE_OPENCODE)|g; \
	     s|AGENT_IMAGE_CRUSH_VALUE|$(AGENT_IMAGE_CRUSH)|g; \
	     s|MAX_CONCURRENT_AGENTS_VALUE|$$MAX_VAL|g; \
	     s|OPENSHELL_GATEWAY_URL_VALUE|$$OPENSHELL_GW|g" \
	  k8s/swarmer/deployment.yaml | kubectl apply -f -
	@# ── 6. Wait for rollout ─────────────────────────────────────────────────
	kubectl rollout status deployment/swarmer -n $(NAMESPACE) --timeout=120s
	@echo ""
	@echo "✓ Swarmer deployed."
	@set -e; \
	ROUTE=$$(kubectl get route swarmer -n $(NAMESPACE) -o jsonpath='{.spec.host}' 2>/dev/null || true); \
	if [ -n "$$ROUTE" ]; then \
	  echo "  Dashboard → https://$$ROUTE"; \
	else \
	  NODE_IP=$$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null); \
	  if [ -z "$$NODE_IP" ]; then \
	    NODE_IP=$$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null); \
	  fi; \
	  if [ -n "$$NODE_IP" ]; then \
	    echo "  Dashboard → http://$$NODE_IP:30080"; \
	  else \
	    echo "  Run 'make connect' to access the dashboard (port-forward to localhost:$(LOCAL_PORT))."; \
	  fi; \
	fi

delete:  ## Remove swarmer and OpenShell from the current kubectl context
	@echo "Removing swarmer from namespace $(NAMESPACE)..."
	kubectl delete -f k8s/swarmer/deployment.yaml --ignore-not-found 2>/dev/null || true
	kubectl delete -f k8s/swarmer/service.yaml --ignore-not-found
	kubectl delete -f k8s/openshift/service.yaml --ignore-not-found 2>/dev/null || true
	kubectl delete -f k8s/openshift/route.yaml --ignore-not-found 2>/dev/null || true
	kubectl delete -f k8s/openshift/oauth-client.yaml --ignore-not-found 2>/dev/null || true
	kubectl delete -f k8s/swarmer/pvc.yaml --ignore-not-found
	kubectl delete secret swarmer-secret -n $(NAMESPACE) --ignore-not-found
	kubectl delete secret openshell-tls -n $(NAMESPACE) --ignore-not-found
	kubectl delete -f k8s/swarmer/rbac.yaml --ignore-not-found
	kubectl delete -f k8s/swarmer/namespace.yaml --ignore-not-found
	@echo "✓ Swarmer removed."
	@echo "Removing OpenShell from namespace $(OPENSHELL_NAMESPACE)..."
	helm uninstall openshell -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true
	kubectl delete namespace $(OPENSHELL_NAMESPACE) --ignore-not-found
	@echo "✓ OpenShell removed."

connect:  ## Port-forward the swarmer dashboard to localhost:$(LOCAL_PORT)
	@echo "Forwarding http://localhost:$(LOCAL_PORT) → swarmer service..."
	kubectl port-forward -n $(NAMESPACE) service/swarmer $(LOCAL_PORT):8080

openshell-register:  ## Register (or refresh) the active cluster's OpenShell gateway in the local CLI
	@# Derive a stable gateway name from the current kubectl context
	@CTX=$$(kubectl config current-context 2>/dev/null || echo "unknown"); \
	GW_NAME=$$(echo "$$CTX" | tr '/:.' '-' | sed 's/[^a-zA-Z0-9_-]/-/g' | sed 's/^-*//'); \
	GW_DIR="$(HOME)/.config/openshell/gateways/$$GW_NAME"; \
	CERTS="$(OPENSHELL_TLS_DIR)"; \
	\
	# If gateway already registered, reuse its existing port to avoid drift. \
	# Only pick a new port for fresh registrations. \
	if [ -f "$$GW_DIR/metadata.json" ]; then \
	  PORT=$$(python3 -c "import json; d=json.load(open('$$GW_DIR/metadata.json')); ep=d.get('gateway_endpoint',''); print(ep.rsplit(':',1)[-1] if ':' in ep else '$(OS_LOCAL_PORT)')" 2>/dev/null); \
	  PORT=$${PORT:-$(OS_LOCAL_PORT)}; \
	else \
	  # Auto-select a free port: skip ports used by other gateways OR already bound on localhost \
	  PORT=$(OS_LOCAL_PORT); \
	  while true; do \
	    USED=0; \
	    for meta in $(HOME)/.config/openshell/gateways/*/metadata.json; do \
	      [ -f "$$meta" ] || continue; \
	      GNAME=$$(python3 -c "import json; d=json.load(open('$$meta')); print(d.get('name',''))" 2>/dev/null); \
	      [ "$$GNAME" = "$$GW_NAME" ] && continue; \
	      P=$$(python3 -c "import json; d=json.load(open('$$meta')); ep=d.get('gateway_endpoint',''); print(ep.rsplit(':',1)[-1] if ':' in ep else '')" 2>/dev/null); \
	      [ "$$P" = "$$PORT" ] && USED=1 && break; \
	    done; \
	    if [ "$$USED" -eq 0 ]; then \
	      python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',$$PORT)); s.close()" 2>/dev/null || USED=1; \
	    fi; \
	    [ "$$USED" -eq 0 ] && break; \
	    PORT=$$((PORT + 1)); \
	  done; \
	fi; \
	\
	echo "Registering gateway '$$GW_NAME' on localhost:$$PORT (context: $$CTX)"; \
	\
	# Create or update the gateway registration \
	if [ ! -f "$$GW_DIR/metadata.json" ]; then \
	  openshell gateway add https://localhost:$$PORT --local --name "$$GW_NAME" 2>&1 || true; \
	else \
	  echo "  Gateway '$$GW_NAME' already registered — refreshing certs (port unchanged: $$PORT)."; \
	fi; \
	\
	# Copy this cluster's certs only — never cross-contaminate with other clusters \
	if [ -f "$$CERTS/ca.crt" ]; then \
	  mkdir -p "$$GW_DIR/mtls"; \
	  cp "$$CERTS/ca.crt"  "$$GW_DIR/mtls/ca.crt"; \
	  cp "$$CERTS/tls.crt" "$$GW_DIR/mtls/tls.crt"; \
	  cp "$$CERTS/tls.key" "$$GW_DIR/mtls/tls.key"; \
	  echo "  ✓ mTLS certs installed for '$$GW_NAME'"; \
	else \
	  echo "  ⚠ No certs in $(OPENSHELL_TLS_DIR) — run 'make deploy' first"; \
	fi; \
	\
	# Save kubectl context so connect-openshell can forward the right cluster later \
	mkdir -p "$$GW_DIR"; \
	echo "$$CTX" > "$$GW_DIR/kubectl_context"; \
	\
	# Switch active gateway to this cluster \
	openshell gateway select "$$GW_NAME" 2>/dev/null || \
	  echo "$$GW_NAME" > "$(HOME)/.config/openshell/active_gateway"; \
	echo "  ✓ Active gateway set to '$$GW_NAME'"

connect-openshell:  ## Port-forward every registered OpenShell gateway (Ctrl-C stops all)
	python3 scripts/openshell_connect.py --namespace $(OPENSHELL_NAMESPACE)

status:  ## Show OpenShell and swarmer deployment status
	@echo "=== Helm release ==="
	@helm status openshell -n $(OPENSHELL_NAMESPACE) 2>/dev/null || echo "  (not installed)"
	@echo ""
	@echo "=== Gateway pods ==="
	@kubectl get pods -n $(OPENSHELL_NAMESPACE) 2>/dev/null || echo "  (namespace not found)"
	@echo ""
	@echo "=== Agent Sandbox CRDs ==="
	@kubectl get crds | grep -i sandbox 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "=== mTLS certs ==="
	@ls -la $(OPENSHELL_TLS_DIR)/ 2>/dev/null || echo "  (not extracted — run 'make deploy' to install OpenShell)"
	@echo ""
	@echo "=== Swarmer deployment ==="
	@kubectl get deployment swarmer -n $(NAMESPACE) 2>/dev/null || echo "  (not deployed)"

# ──────────────────────────────────────────────────────────────
#  kind (local development cluster)
# ──────────────────────────────────────────────────────────────

kind-deploy:  ## One-shot local dev: create kind cluster + build + load image + deploy swarmer
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first." && exit 1)
	@# Create cluster (idempotent)
	@if kind get clusters 2>/dev/null | grep -q "^$(KIND_CLUSTER)$$"; then \
	  echo "kind cluster '$(KIND_CLUSTER)' already exists — skipping creation."; \
	else \
	  kind create cluster --name $(KIND_CLUSTER) --config k8s/kind-config.yaml; \
	  echo "✓ kind cluster '$(KIND_CLUSTER)' created."; \
	fi
	@# Build and side-load image (no registry needed)
	$(MAKE) image-build SILENT=1
	@echo "Loading $(IMAGE_REF) into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save $(IMAGE_REF) | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image $(IMAGE_REF) --name $(KIND_CLUSTER); \
	fi
	@echo "✓ Image loaded."
	$(MAKE) deploy SILENT=1
	@echo ""
	@echo "╔══════════════════════════════════════════════════════╗"
	@echo "║  Swarmer is running in kind!                         ║"
	@echo "╚══════════════════════════════════════════════════════╝"
	@echo "  Dashboard → http://localhost:$(LOCAL_PORT)"

kind-delete:  ## Delete the kind cluster (removes all data inside it)
	kind delete cluster --name $(KIND_CLUSTER)
	@echo "✓ kind cluster '$(KIND_CLUSTER)' deleted."

# ──────────────────────────────────────────────────────────────
#  Help
# ──────────────────────────────────────────────────────────────

help:  ## Show this help
	@echo "Swarmer Makefile targets:"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' Makefile \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' \
	  | sort
	@echo ""
	@echo "Variables (override on CLI or in .env):"
	@echo "  IMAGE=$(IMAGE)  IMAGE_TAG=$(IMAGE_TAG)  REGISTRY=$(REGISTRY)"
	@echo "  CONTAINER_CMD=$(CONTAINER_CMD)  KIND_CLUSTER=$(KIND_CLUSTER)"
	@echo "  NAMESPACE=$(NAMESPACE)  LOCAL_PORT=$(LOCAL_PORT)"
	@echo "  TOKEN_DURATION=$(TOKEN_DURATION)  AGENT_IMAGE_OPENCODE  AGENT_IMAGE_CRUSH"
	@echo ""
	@echo "Notes:"
	@echo "  Reset DB: delete data/swarmer.db (fresh schema created on next start)"

.DEFAULT_GOAL := help
