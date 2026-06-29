"""
Port-forward all registered OpenShell gateways concurrently.

For each gateway that has a metadata.json, check if the port is already
reachable. If not, start a kubectl port-forward in the background using
the kubectl context stored in the gateway's kubectl_context file (written
by `make openshell-register`). All forwards run until Ctrl-C.

Usage:
    python3 scripts/openshell_connect.py [--namespace NAMESPACE]
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--namespace", default=os.environ.get("OPENSHELL_NAMESPACE", "openshell"))
args = parser.parse_args()

gw_base = Path.home() / ".config/openshell/gateways"
active_file = Path.home() / ".config/openshell/active_gateway"
active = active_file.read_text().strip() if active_file.exists() else ""

procs: list[subprocess.Popen] = []


def port_reachable(port: str) -> bool:
    try:
        s = socket.socket()
        s.settimeout(0.3)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except Exception:
        return False


def cleanup(sig=None, frame=None):
    print("\nStopping port-forwards...")
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

for meta_path in sorted(gw_base.glob("*/metadata.json")):
    try:
        d = json.loads(meta_path.read_text())
        name = d.get("name", "")
        ep = d.get("gateway_endpoint", "")
        port = ep.rsplit(":", 1)[-1] if ":" in ep else ""
        ctx_file = meta_path.parent / "kubectl_context"
        ctx = ctx_file.read_text().strip() if ctx_file.exists() else ""
    except Exception:
        continue

    if not name or not port:
        continue

    marker = "* " if name == active else "  "

    if port_reachable(port):
        print(f"{marker}[{name}] port {port} already reachable — skipping port-forward")
        continue

    cmd = ["kubectl", "port-forward", "-n", args.namespace, "svc/openshell", f"{port}:8080"]
    if ctx:
        cmd = ["kubectl", "--context", ctx, "port-forward", "-n", args.namespace,
               "svc/openshell", f"{port}:8080"]

    ctx_label = f"  (context: {ctx})" if ctx else "  (active kubectl context)"
    print(f"{marker}[{name}] forwarding localhost:{port} → openshell:8080{ctx_label}")
    procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

if not procs:
    print("All gateway ports already reachable (or no gateways registered).")
    print("Run 'openshell gateway list' to see registered gateways.")
    sys.exit(0)

print(f"\nActive gateway : {active or '(none)'}")
print("Press Ctrl-C to stop all port-forwards.\n")

while True:
    time.sleep(5)
    dead = [p for p in procs if p.poll() is not None]
    for p in dead:
        print(f"[warn] port-forward pid {p.pid} exited unexpectedly (rc={p.returncode})")
    procs = [p for p in procs if p.poll() is None]
    if not procs:
        print("All port-forwards have exited.")
        break
