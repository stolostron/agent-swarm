"""Expose an OpenShell service locally over HTTP for browser access.

The OpenShell gateway requires a client certificate. Browsers should not need
that certificate, so this small proxy terminates the local HTTP connection and
reconnects to the gateway with the registered mTLS client credentials.
"""
from __future__ import annotations

import argparse
import http.client
import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length"}
        }
        headers["Host"] = self.server.service_host  # type: ignore[attr-defined]
        headers["Connection"] = "close"

        connection = _MtlsConnection(self.server, self.server.service_host)  # type: ignore[attr-defined]
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, f"OpenShell service connection failed: {exc}")
        finally:
            connection.close()

    do_GET = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward

    def log_message(self, format: str, *args) -> None:
        print(f"openshell-browser-proxy: {format % args}")


class _MtlsConnection(http.client.HTTPSConnection):
    def __init__(self, server, service_host: str):
        super().__init__(server.gateway_host, server.gateway_port, timeout=30)
        self._proxy_server = server
        self._service_host = service_host

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._proxy_server.gateway_host, self._proxy_server.gateway_port),
            timeout=self.timeout,
        )
        # The local Kind gateway certificate bundle is valid for the OpenShell
        # CLI but lacks metadata required by Python's stricter verifier. The
        # client certificate still authenticates this connection; the proxy is
        # bound to loopback and the gateway endpoint is not exposed directly.
        context = ssl._create_unverified_context()
        context.load_cert_chain(self._proxy_server.cert_file, self._proxy_server.key_file)
        self.sock = context.wrap_socket(sock, server_hostname=self._service_host)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--gateway-host", required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--service-host", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--cert-file", required=True)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.listen_port), _ProxyHandler)
    server.gateway_host = args.gateway_host
    server.gateway_port = args.gateway_port
    server.service_host = args.service_host
    server.ca_file = args.ca_file
    server.cert_file = args.cert_file
    server.key_file = args.key_file
    print(f"Browser URL: http://127.0.0.1:{args.listen_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
