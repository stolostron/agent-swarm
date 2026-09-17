"""Exercise the complete local KinD deployment lifecycle.

This is intentionally a standalone smoke test rather than a pytest test. It
creates a fresh KinD cluster, runs ``make kind-deploy``, authenticates against
the deployed application with a ServiceAccount token issued by
``make user-token``, and destroys the cluster afterward.

Usage::

    python3 scripts/e2e_kind_deploy.py

The cluster is deleted on failure too. Use ``--keep-cluster`` to retain it
for debugging.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://127.0.0.1:8080"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+/=-]{20,}")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def log_step(name: str, passed: bool, detail: str = "") -> None:
    marker = "PASS" if passed else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{marker}] {name}{suffix}")


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}")
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def require_commands(commands: list[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"missing required commands: {', '.join(missing)}")


def assert_port_available(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(f"host port {port} is unavailable: {exc}") from exc
    finally:
        sock.close()


def cluster_exists(cluster: str) -> bool:
    result = run(["kind", "get", "clusters"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not query KinD clusters")
    return cluster in result.stdout.splitlines()


def extract_token(output: str) -> str:
    for line in output.splitlines():
        candidate = line.strip()
        match = TOKEN_PATTERN.match(candidate)
        if match:
            return match.group(0)
    raise RuntimeError("make user-token did not emit a recognizable token")


def request(url: str, token: str | None = None, form: str | None = None) -> tuple[int, str, str]:
    headers = {"Accept": "text/html,application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = form.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    opener = build_opener(_NoRedirects)
    try:
        response = opener.open(req, timeout=10)
        return response.status, response.geturl(), response.read().decode(errors="replace")
    except HTTPError as exc:
        location = exc.headers.get("Location", exc.geturl())
        if location.startswith("/"):
            location = urljoin(req.full_url, location)
        return exc.code, location, exc.read().decode(errors="replace")
    except URLError as exc:
        raise RuntimeError(f"request to {url} failed: {exc.reason}") from exc


def wait_for_http(url: str, timeout: int) -> tuple[int, str, str]:
    deadline = time.monotonic() + timeout
    last_error = "service did not respond"
    while time.monotonic() < deadline:
        try:
            return request(url)
        except RuntimeError as exc:
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(last_error)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default=os.getenv("KIND_CLUSTER", "swarmer"))
    parser.add_argument("--user", default="e2e-tester")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--keep-cluster", action="store_true")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    make = ["make", f"KIND_CLUSTER={args.cluster_name}"]
    cleanup_needed = False
    failed = False

    try:
        require_commands(
            ["make", "kind", "kubectl", "helm", os.getenv("CONTAINER_CMD", "podman")]
        )
        assert_port_available(8080)
        if cluster_exists(args.cluster_name):
            raise RuntimeError(
                f"KinD cluster '{args.cluster_name}' already exists; delete it first"
            )
        log_step("prerequisites", True)

        cleanup_needed = True
        deploy = run(make + ["kind-deploy", "SILENT=1"], timeout=args.timeout)
        if deploy.returncode != 0:
            raise RuntimeError(deploy.stdout + deploy.stderr)
        log_step("make kind-deploy", True)

        rollout = run(
            [
                "kubectl",
                "rollout",
                "status",
                "deployment/swarmer",
                "-n",
                "swarmer",
                f"--timeout={args.timeout}s",
            ],
            timeout=args.timeout + 30,
        )
        if rollout.returncode != 0:
            raise RuntimeError(rollout.stdout + rollout.stderr)
        log_step("Swarmer rollout", True)

        token_result = run(make + ["user-token", f"SA_USER={args.user}"], timeout=60)
        if token_result.returncode != 0:
            raise RuntimeError(token_result.stdout + token_result.stderr)
        token = extract_token(token_result.stdout)
        log_step("make user-token", True)

        status, _, _ = wait_for_http(f"{args.url.rstrip('/')}/login", args.timeout)
        if status != 200:
            raise RuntimeError(f"/login returned HTTP {status}")
        log_step("HTTP connectivity", True)

        status, _, body = request(f"{args.url.rstrip('/')}/api/v1/me", token=token)
        expected_user = f"system:serviceaccount:swarmer:{args.user}"
        if status != 200 or expected_user not in body:
            raise RuntimeError(f"/api/v1/me returned HTTP {status}: {body[:300]}")
        log_step("bearer token authentication", True)

        status, _, _ = request(f"{args.url.rstrip('/')}/api/v1/workspaces", token=token)
        if status != 200:
            raise RuntimeError(f"/api/v1/workspaces returned HTTP {status}")
        log_step("authenticated workspace API", True)

        status, location, _ = request(
            f"{args.url.rstrip('/')}/login", form=urlencode({"token": token})
        )
        if status != 303 or not location.endswith("/workspaces"):
            raise RuntimeError(f"form login returned HTTP {status}, location {location}")
        log_step("console token login", True)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        failed = True
        log_step("lifecycle verification", False, str(exc).strip())
    finally:
        if cleanup_needed and not args.keep_cluster:
            destroy = run(make + ["kind-destroy"], timeout=120)
            if destroy.returncode == 0 and not cluster_exists(args.cluster_name):
                log_step("make kind-destroy", True)
            else:
                failed = True
                log_step("make kind-destroy", False, destroy.stdout + destroy.stderr)
        elif cleanup_needed:
            print(f"Cluster '{args.cluster_name}' retained for debugging.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
