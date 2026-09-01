"""
OIDC bearer-token authentication for remote/hosted OpenShell gateways (ACM-41655).

Supports multi-tenant per-workspace OIDC auto-refresh and thread-safe DB write-back
without requiring pod restarts or storing plaintext credentials on disk.

Flow:
  1. User/admin configures a workspace gateway with OIDC issuer, client_id, audience,
     and initial refresh token (via UI, API, or command parser).
  2. An in-memory OidcGatewayAuth instance is created for that workspace.
  3. current_access_token() is handed as a zero-arg callable to SandboxClient(bearer_token=...).
     The OpenShell SDK calls it before every gRPC RPC. It only makes an HTTP call to the IdP
     when the cached access token is near expiry.
  4. Refreshed token bundles (including rotated refresh tokens) are written back encrypted
     to the database via asyncio.run_coroutine_threadsafe onto the main event loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Refresh this many seconds before actual expiry so a slow RPC doesn't race past expiry.
_EXPIRY_GRACE_SECONDS = 60
# Timeout for best-effort DB write-back of a rotated refresh token.
_WRITE_BACK_TIMEOUT = 5.0


class OidcAuthError(RuntimeError):
    """Raised when OIDC discovery/refresh fails or the credential is missing."""


class _InvalidGrantError(OidcAuthError):
    """The IdP rejected the refresh_token (expired, revoked, or rotated away)."""


class OidcGatewayAuth:
    """In-process, lock-coordinated OAuth2 refresh for an OpenShell gateway credential."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        audience: str = "",
        workspace_id: int | None = None,
        tls_ca: str | None = None,
    ):
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._audience = audience
        self._workspace_id = workspace_id
        self._tls_ca = tls_ca
        self._lock = threading.Lock()
        self._bundle: dict[str, Any] | None = None
        self._token_endpoint: str | None = None
        self._http = httpx.Client(
            follow_redirects=False,
            timeout=15.0,
            verify=_httpx_verify_arg(tls_ca),
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    def close(self) -> None:
        self._http.close()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def seed(
        self,
        refresh_token: str,
        access_token: str = "",
        expires_at: int | None = None,
    ) -> None:
        """Populate the in-memory token bundle."""
        with self._lock:
            self._bundle = {
                "refresh_token": refresh_token,
                "access_token": access_token,
                "expires_at": expires_at,
            }

    def current_access_token(self) -> str:
        """Zero-arg callable passed to SandboxClient(bearer_token=...).

        Called by the OpenShell SDK's gRPC interceptor before every RPC.
        Must return quickly; only refreshes against the IdP when stale.
        """
        with self._lock:
            if self._bundle is None or not self._bundle.get("refresh_token"):
                if not self._reload_from_db():
                    ws_info = f" for workspace {self._workspace_id}" if self._workspace_id else ""
                    raise OidcAuthError(
                        f"OpenShell OIDC credential not configured{ws_info} — provide a refresh token in gateway settings."
                    )
            if self._is_fresh(self._bundle):
                return self._bundle["access_token"]
            try:
                self._bundle = self._refresh(self._bundle)
            except _InvalidGrantError:
                if self._reload_from_db() and self._bundle and self._bundle.get("refresh_token"):
                    try:
                        self._bundle = self._refresh(self._bundle)
                    except _InvalidGrantError:
                        log.error(
                            "OpenShell OIDC refresh_token rejected (invalid_grant) for workspace %s",
                            self._workspace_id,
                        )
                        raise
                else:
                    log.error(
                        "OpenShell OIDC refresh_token rejected (invalid_grant) for workspace %s",
                        self._workspace_id,
                    )
                    raise
            self._write_back(self._bundle)
            return self._bundle["access_token"]

    def _reload_from_db(self) -> bool:
        """Best-effort reload of the credential bundle from the DB."""
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        if self._loop is None:
            return False
        # If running directly on the event loop thread, blocking on .result() would deadlock.
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is not None and current_loop is self._loop:
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _load_workspace_bundle(self._workspace_id), self._loop
            )
            bundle = fut.result(timeout=_WRITE_BACK_TIMEOUT)
        except Exception:
            log.warning("Failed to reload OpenShell OIDC credential from DB for ws %s", self._workspace_id, exc_info=True)
            return False
        if bundle is None:
            return False
        self._bundle = bundle
        return True

    @staticmethod
    def _is_fresh(bundle: dict) -> bool:
        access_token = bundle.get("access_token")
        if not access_token:
            return False
        exp = bundle.get("expires_at")
        if exp is None:
            return True
        return int(time.time()) + _EXPIRY_GRACE_SECONDS < exp

    def _discover_token_endpoint(self) -> str:
        if self._token_endpoint is not None:
            return self._token_endpoint
        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            resp = self._http.get(discovery_url)
        except httpx.HTTPError as e:
            raise OidcAuthError(f"OIDC discovery failed for {self._issuer}: {e}") from e
        if not 200 <= resp.status_code < 300:
            raise OidcAuthError(
                f"OIDC discovery failed: HTTP {resp.status_code} from {discovery_url}"
            )
        disco = resp.json()
        discovered_issuer = str(disco.get("issuer", "")).rstrip("/")
        if discovered_issuer != self._issuer:
            raise OidcAuthError(
                f"OIDC discovery issuer mismatch: expected '{self._issuer}', got '{discovered_issuer}'"
            )
        endpoint = disco.get("token_endpoint")
        if not endpoint:
            raise OidcAuthError("OIDC discovery response missing token_endpoint")
        self._token_endpoint = endpoint
        return endpoint

    def _refresh(self, bundle: dict) -> dict:
        token_endpoint = self._discover_token_endpoint()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "client_id": self._client_id,
        }
        if self._audience:
            data["audience"] = self._audience
        try:
            resp = self._http.post(token_endpoint, data=data)
        except httpx.HTTPError as e:
            raise OidcAuthError(f"OIDC token refresh failed: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            error_code = None
            with contextlib.suppress(Exception):
                error_code = resp.json().get("error")
            if error_code == "invalid_grant":
                raise _InvalidGrantError(f"OIDC refresh rejected: {resp.text[:200]}")
            raise OidcAuthError(
                f"OIDC token refresh failed: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        token = resp.json()
        access_token = token.get("access_token")
        if not access_token:
            raise OidcAuthError("OIDC refresh response missing access_token")
        expires_at = token.get("expires_at")
        if expires_at is None:
            expires_in = token.get("expires_in")
            if isinstance(expires_in, (int, float)):
                expires_at = int(time.time()) + int(expires_in)
        return {
            "access_token": access_token,
            "refresh_token": token.get("refresh_token", bundle["refresh_token"]),
            "expires_at": int(expires_at) if expires_at is not None else None,
        }

    def _write_back(self, bundle: dict) -> None:
        """Best-effort persist of the refreshed bundle to the DB."""
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        if self._loop is None:
            log.warning("OIDC token refreshed but no event loop registered — not persisted to DB")
            return
        try:
            current_loop = None
            with contextlib.suppress(RuntimeError):
                current_loop = asyncio.get_running_loop()
            if current_loop is not None and current_loop is self._loop:
                self._loop.create_task(_persist_workspace_bundle(self._workspace_id, bundle))
            else:
                fut = asyncio.run_coroutine_threadsafe(
                    _persist_workspace_bundle(self._workspace_id, bundle), self._loop
                )
                fut.result(timeout=_WRITE_BACK_TIMEOUT)
        except Exception:
            log.warning(
                "Failed to persist refreshed OpenShell OIDC token for ws %s to DB",
                self._workspace_id,
                exc_info=True,
            )


class WorkspaceOidcAuthManager:
    """Registry of in-memory OidcGatewayAuth instances keyed by workspace_id."""

    def __init__(self) -> None:
        self._instances: dict[int, OidcGatewayAuth] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        workspace_id: int,
        issuer: str,
        client_id: str,
        audience: str = "",
        refresh_token: str = "",
        access_token: str = "",
        expires_at: int | None = None,
        tls_ca: str | None = None,
    ) -> OidcGatewayAuth:
        with self._lock:
            existing = self._instances.get(workspace_id)
            if (
                existing is not None
                and existing._issuer == issuer.rstrip("/")
                and existing._client_id == client_id
                and existing._audience == audience
            ):
                if refresh_token:
                    existing.seed(refresh_token, access_token, expires_at)
                return existing

            if existing is not None:
                existing.close()

            auth = OidcGatewayAuth(
                issuer=issuer,
                client_id=client_id,
                audience=audience,
                workspace_id=workspace_id,
                tls_ca=tls_ca,
            )
            with contextlib.suppress(RuntimeError):
                auth.set_event_loop(asyncio.get_running_loop())
            if refresh_token:
                auth.seed(refresh_token, access_token, expires_at)
            self._instances[workspace_id] = auth
            return auth

    def invalidate(self, workspace_id: int) -> None:
        with self._lock:
            existing = self._instances.pop(workspace_id, None)
            if existing is not None:
                existing.close()

    def clear(self) -> None:
        with self._lock:
            for inst in self._instances.values():
                inst.close()
            self._instances.clear()


oidc_manager = WorkspaceOidcAuthManager()


def _httpx_verify_arg(tls_ca: str | None) -> bool | str | ssl.SSLContext:
    """Build a safe httpx verify argument from workspace CA settings.

    Workspace gateway rows can store `tls_ca` either as a filesystem path
    (global/default shape) or as inline PEM content (per-workspace DB shape).
    httpx accepts a bool, a CA bundle path, or an SSLContext. Passing inline
    PEM directly as a string makes httpx treat it as a path and fail.
    """
    if not tls_ca:
        return True
    ca = tls_ca.strip()
    if not ca:
        return True
    try:
        p = pathlib.Path(ca)
        if p.exists():
            return str(p)
    except OSError:
        pass

    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=ca)
    return ctx


async def _load_workspace_bundle(workspace_id: int | None) -> dict | None:
    if workspace_id is None:
        return None

    from sqlalchemy import select
    from swarmer.database import get_db
    from swarmer.models.workspace_gateway import WorkspaceGateway

    async for db in get_db():
        gw = (
            await db.execute(
                select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()
        if gw is None or not gw.refresh_token:
            return None
        expires_at = (
            int(gw.access_token_expires_at.replace(tzinfo=timezone.utc).timestamp())
            if gw.access_token_expires_at
            else None
        )
        return {
            "refresh_token": gw.refresh_token,
            "access_token": gw.access_token,
            "expires_at": expires_at,
        }
    return None


async def _persist_workspace_bundle(workspace_id: int | None, bundle: dict) -> None:
    if workspace_id is None:
        return

    from sqlalchemy import select
    from swarmer.database import get_db
    from swarmer.models.workspace_gateway import WorkspaceGateway

    async for db in get_db():
        gw = (
            await db.execute(
                select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()
        if gw is not None:
            gw.refresh_token = bundle["refresh_token"]
            gw.access_token = bundle.get("access_token", "")
            expires_at = bundle.get("expires_at")
            gw.access_token_expires_at = (
                datetime.fromtimestamp(expires_at, tz=timezone.utc).replace(tzinfo=None)
                if expires_at
                else None
            )
            await db.commit()
        break
