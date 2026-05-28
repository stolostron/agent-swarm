"""K8s bearer token resolution for the Agent Swarm MCP server.

Resolution order:
1. AGENT_SWARM_API_TOKEN env var (explicit override)
2. In-cluster service account token (pod sidecar deployment)
3. Kubeconfig extraction (laptop after oc login / kubectl login)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

_IN_CLUSTER_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


def resolve_token() -> str:
    """Return a K8s bearer token from the best available source.

    Raises RuntimeError if no token can be found.
    """
    token = (
        _from_env()
        or _from_in_cluster()
        or _from_kubeconfig()
    )
    if not token:
        raise RuntimeError(
            "No K8s bearer token found. Set AGENT_SWARM_API_TOKEN, "
            "run 'oc login' / 'kubectl login', or deploy as a pod sidecar."
        )
    return token


def _from_env() -> Optional[str]:
    token = os.environ.get("AGENT_SWARM_API_TOKEN", "").strip()
    if token:
        log.debug("auth: using AGENT_SWARM_API_TOKEN env var")
        return token
    return None


def _from_in_cluster() -> Optional[str]:
    if _IN_CLUSTER_TOKEN.exists():
        try:
            token = _IN_CLUSTER_TOKEN.read_text().strip()
        except OSError as e:
            log.warning("auth: failed reading in-cluster token: %s", e)
            return None
        if token:
            log.debug("auth: using in-cluster service account token")
            return token
    return None


def _from_kubeconfig() -> Optional[str]:
    kubeconfig_path = _resolve_kubeconfig_path()
    if not kubeconfig_path or not kubeconfig_path.exists():
        return None

    try:
        config = yaml.safe_load(kubeconfig_path.read_text())
    except Exception as e:
        log.warning("auth: failed to parse kubeconfig: %s", e)
        return None

    if not isinstance(config, dict):
        log.warning("auth: kubeconfig is empty or not a mapping")
        return None

    current_context = config.get("current-context")
    if not current_context:
        log.warning("auth: kubeconfig has no current-context")
        return None

    contexts = {
        c.get("name"): c.get("context", {})
        for c in (config.get("contexts") or [])
        if isinstance(c, dict) and c.get("name")
    }
    ctx = contexts.get(current_context)
    if not ctx:
        log.warning("auth: current-context '%s' not found in kubeconfig", current_context)
        return None

    user_name = ctx.get("user", "")
    users = {
        u.get("name"): u.get("user", {})
        for u in (config.get("users") or [])
        if isinstance(u, dict) and u.get("name")
    }
    user = users.get(user_name, {})

    # Direct token field
    token = user.get("token", "").strip()
    if token:
        log.debug("auth: using token from kubeconfig user '%s'", user_name)
        return token

    # Exec-based credential provider (common with oc login)
    exec_config = user.get("exec")
    if exec_config:
        token = _exec_credential_provider(exec_config)
        if token:
            log.debug("auth: using token from kubeconfig exec provider for user '%s'", user_name)
            return token

    log.warning(
        "auth: kubeconfig user '%s' has no token or supported exec provider. "
        "Set AGENT_SWARM_API_TOKEN instead.",
        user_name,
    )
    return None


def _resolve_kubeconfig_path() -> Optional[Path]:
    kubeconfig_env = os.environ.get("KUBECONFIG", "").strip()
    if kubeconfig_env:
        # KUBECONFIG can be a colon-separated list; use the first one
        first = kubeconfig_env.split(":")[0]
        return Path(first)
    default = Path.home() / ".kube" / "config"
    if default.exists():
        return default
    return None


def _exec_credential_provider(exec_config: dict) -> Optional[str]:
    """Run an exec credential provider and extract the token from its output."""
    command = exec_config.get("command")
    if not command:
        return None

    args = [command] + (exec_config.get("args") or [])
    env = os.environ.copy()
    for kv in exec_config.get("env") or []:
        if kv and kv.get("name"):
            env[kv["name"]] = kv.get("value", "")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning("auth: exec provider '%s' exited %d: %s", command, result.returncode, result.stderr)
            return None

        import json
        data = json.loads(result.stdout)
        token = data.get("status", {}).get("token", "").strip()
        return token or None
    except Exception as e:
        log.warning("auth: exec provider '%s' failed: %s", command, e)
        return None
