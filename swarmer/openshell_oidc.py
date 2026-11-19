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
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Refresh this many seconds before actual expiry so a slow RPC doesn't race past expiry.
_EXPIRY_GRACE_SECONDS = 60
# Timeout for best-effort DB write-back of a rotated refresh token.
_WRITE_BACK_TIMEOUT = 5.0


class OidcAuthError(RuntimeError):
    """Raised when OIDC discovery/refresh fails or the credential is missing."""


class StaleGatewayConfigurationError(RuntimeError):
    """A gateway snapshot lost the race to a newer configuration revision."""


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
        client_secret: str = "",
        service_account_subject: str = "",
        tls_ca: str | None = None,
        tls_verify: bool = True,
        configuration_revision: int = 0,
    ):
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._audience = audience
        self._workspace_id = workspace_id
        self._client_secret = client_secret
        self._service_account_subject = service_account_subject
        self._tls_ca = tls_ca
        self._tls_verify = tls_verify
        self._configuration_revision = configuration_revision
        self._lock = threading.Lock()
        self._bundle: dict[str, Any] | None = None
        self._pending_writeback = None
        self._deferred_writeback: tuple[dict, int] | None = None
        self._token_endpoint: str | None = None
        self._http = httpx.Client(
            follow_redirects=False,
            timeout=15.0,
            verify=_httpx_verify_arg(tls_ca) if tls_verify else False,
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    def close(self) -> None:
        self._http.close()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def seed(
        self,
        refresh_token: str = "",
        access_token: str = "",
        expires_at: int | None = None,
        client_secret: str = "",
        service_account_subject: str = "",
    ) -> None:
        """Populate the in-memory token bundle."""
        with self._lock:
            self._seed_unlocked(
                refresh_token,
                access_token,
                expires_at,
                client_secret,
                service_account_subject,
            )

    def _seed_unlocked(
        self,
        refresh_token: str,
        access_token: str,
        expires_at: int | None,
        client_secret: str,
        service_account_subject: str,
    ) -> None:
        """Update credential state while the caller owns ``_lock``."""
        if client_secret:
            self._client_secret = client_secret
        if service_account_subject:
            self._service_account_subject = service_account_subject
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
            if self._client_secret:
                if self._bundle and self._is_fresh(self._bundle):
                    return self._bundle["access_token"]
                self._bundle = self._fetch_client_credentials()
                self._write_back(self._bundle)
                return self._bundle["access_token"]

            if self._bundle is None or not self._bundle.get("refresh_token"):
                if not self._reload_from_db():
                    ws_info = f" for workspace {self._workspace_id}" if self._workspace_id else ""
                    raise OidcAuthError(
                        f"OpenShell OIDC credential not configured{ws_info} — provide a refresh token or client secret in gateway settings."
                    )
                if self._client_secret:
                    if self._bundle and self._is_fresh(self._bundle):
                        return self._bundle["access_token"]
                    self._bundle = self._fetch_client_credentials()
                    self._write_back(self._bundle)
                    return self._bundle["access_token"]
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

    async def current_access_token_async(self) -> str:
        """Refresh tokens off the event-loop thread for HTTP proxy callers."""
        return await asyncio.to_thread(self.current_access_token)

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
        if (
            bundle.get("configuration_revision", self._configuration_revision)
            != self._configuration_revision
        ):
            # The database now describes a different credential/configuration
            # generation. Let the next gateway-config resolution replace this
            # auth object instead of pairing new secrets with stale OIDC data.
            return False
        if bundle.get("client_secret"):
            self._client_secret = bundle["client_secret"]
        if bundle.get("service_account_subject"):
            self._service_account_subject = bundle["service_account_subject"]
        self._configuration_revision = bundle.get(
            "configuration_revision", self._configuration_revision
        )
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
        issuer_parsed = urlparse(self._issuer)
        if issuer_parsed.scheme != "https" and issuer_parsed.hostname not in (
            "localhost", "127.0.0.1", "::1",
        ):
            raise OidcAuthError(
                f"Insecure OIDC issuer '{self._issuer}': HTTPS is required for non-loopback issuers"
            )
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
        endpoint = str(disco.get("token_endpoint", "")).strip()
        if not endpoint:
            raise OidcAuthError("OIDC discovery response missing token_endpoint")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise OidcAuthError(
                f"Insecure OIDC token_endpoint '{endpoint}': HTTPS is required for non-loopback endpoints"
            )
        self._token_endpoint = endpoint
        return endpoint

    def _fetch_client_credentials(self) -> dict:
        token_endpoint = self._discover_token_endpoint()
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._audience:
            data["audience"] = self._audience
        if self._service_account_subject:
            data["subject"] = self._service_account_subject
        try:
            resp = self._http.post(token_endpoint, data=data)
        except httpx.HTTPError as e:
            raise OidcAuthError(f"OIDC client_credentials failed: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise OidcAuthError(
                f"OIDC client_credentials failed: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        token = resp.json()
        access_token = token.get("access_token")
        if not access_token:
            raise OidcAuthError("OIDC client_credentials response missing access_token")
        expires_at = token.get("expires_at")
        if expires_at is None:
            expires_in = token.get("expires_in")
            if isinstance(expires_in, (int, float)):
                expires_at = int(time.time()) + int(expires_in)
        return {
            "access_token": access_token,
            "refresh_token": "",
            "expires_at": int(expires_at) if expires_at is not None else None,
        }

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
        # current_access_token calls this while holding _lock, so the revision
        # is the generation from which this exact bundle was produced.
        expected_revision = self._configuration_revision
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        if self._loop is None:
            log.warning("OIDC token refreshed but no event loop registered — not persisted to DB")
            return
        pending = self._pending_writeback
        if pending is not None:
            # Preserve the newest rotation while an earlier timed-out write is
            # unresolved. The completion callback serializes this follow-up
            # against the revision actually left in the database.
            self._deferred_writeback = (bundle, expected_revision)
            return
        try:
            current_loop = None
            with contextlib.suppress(RuntimeError):
                current_loop = asyncio.get_running_loop()
            if current_loop is not None and current_loop is self._loop:
                self._loop.create_task(
                    self._persist_and_advance_revision(bundle, expected_revision)
                )
            else:
                fut = asyncio.run_coroutine_threadsafe(
                    _persist_workspace_bundle(
                        self._workspace_id, bundle, expected_revision
                    ),
                    self._loop,
                )
                self._pending_writeback = fut
                try:
                    persisted = fut.result(timeout=_WRITE_BACK_TIMEOUT)
                except TimeoutError:
                    # The DB coroutine may finish after this worker times out.
                    # Reconcile its eventual result rather than allowing an
                    # unobserved commit to strand this auth object at an old
                    # revision.
                    fut.add_done_callback(
                        lambda completed: self._schedule_late_writeback_reconcile(
                            completed, bundle, expected_revision
                        )
                    )
                    log.warning(
                        "OpenShell OIDC token persistence timed out for workspace %s",
                        self._workspace_id,
                    )
                    return
                self._pending_writeback = None
                if persisted:
                    if self._configuration_revision == expected_revision:
                        self._bundle = bundle
                        self._configuration_revision = expected_revision + 1
                else:
                    self._reconcile_writeback_sync(bundle, expected_revision)
        except Exception:
            log.warning(
                "Failed to persist refreshed OpenShell OIDC token for ws %s to DB",
                self._workspace_id,
                exc_info=True,
            )

    async def _persist_and_advance_revision(
        self, bundle: dict, expected_revision: int
    ) -> None:
        persisted = await _persist_workspace_bundle(
            self._workspace_id, bundle, expected_revision
        )
        if persisted:
            # The lock can be contended by a token-refresh worker. Never wait
            # for it on the event loop, and publish only if this token bundle's
            # originating configuration is still current.
            await asyncio.to_thread(
                self._advance_if_current, bundle, expected_revision
            )
        else:
            # A zero-row CAS can mean a prior timed-out write committed. Adopt
            # it only when the DB revision and rotated tokens match this exact
            # write; a newer user configuration remains authoritative.
            await self._reconcile_writeback_async(bundle, expected_revision)

    def _reconcile_writeback_sync(self, bundle: dict, expected_revision: int) -> None:
        if self._loop is None or self._workspace_id is None:
            return
        fut = asyncio.run_coroutine_threadsafe(
            _load_workspace_bundle(self._workspace_id), self._loop
        )
        try:
            current = fut.result(timeout=_WRITE_BACK_TIMEOUT)
        except Exception:
            log.warning("Could not reconcile OpenShell OIDC write-back for workspace %s", self._workspace_id)
            return
        adopted = self._adopt_persisted_write_if_matching(
            current, bundle, expected_revision, lock_already_held=True
        )
        if not adopted:
            log.warning(
                "OpenShell OIDC write-back lost its configuration revision for workspace %s",
                self._workspace_id,
            )

    async def _reconcile_writeback_async(
        self, bundle: dict, expected_revision: int
    ) -> None:
        current = await _load_workspace_bundle(self._workspace_id)
        adopted = await asyncio.to_thread(
            self._adopt_persisted_write_if_matching,
            current,
            bundle,
            expected_revision,
        )
        if not adopted:
            log.warning(
                "OpenShell OIDC write-back lost its configuration revision for workspace %s",
                self._workspace_id,
            )

    def _schedule_late_writeback_reconcile(
        self, completed, bundle: dict, expected_revision: int
    ) -> None:
        try:
            persisted = completed.result()
        except Exception:
            log.warning(
                "Late OpenShell OIDC write-back failed for workspace %s",
                self._workspace_id,
            )
            persisted = False
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(
                self._finish_pending_writeback(
                    completed, bundle, expected_revision, persisted
                )
            )
        )

    async def _finish_pending_writeback(
        self, completed, bundle: dict, expected_revision: int, persisted: bool
    ) -> None:
        try:
            current = await _load_workspace_bundle(self._workspace_id)
        except Exception:
            log.warning(
                "Could not reload OpenShell OIDC state after write-back for workspace %s",
                self._workspace_id,
                exc_info=True,
            )
            await asyncio.to_thread(
                self._take_deferred_writeback, completed, expected_revision, False
            )
            return
        adopted = False
        if persisted:
            adopted = await asyncio.to_thread(
                self._adopt_persisted_write_if_matching,
                current,
                bundle,
                expected_revision,
            )
        else:
            adopted = (
                current is not None
                and current.get("configuration_revision") == expected_revision
                and self._configuration_revision == expected_revision
            )

        deferred = await asyncio.to_thread(
            self._take_deferred_writeback,
            completed,
            expected_revision,
            adopted,
        )
        if deferred is None:
            return
        deferred_bundle, next_revision = deferred
        if current is None or current.get("configuration_revision") != next_revision:
            return
        try:
            if await _persist_workspace_bundle(
                self._workspace_id, deferred_bundle, next_revision
            ):
                await asyncio.to_thread(
                    self._advance_if_current, deferred_bundle, next_revision
                )
            else:
                await self._reconcile_writeback_async(deferred_bundle, next_revision)
        except Exception:
            log.warning(
                "Deferred OpenShell OIDC write-back failed for workspace %s",
                self._workspace_id,
                exc_info=True,
            )

    def _take_deferred_writeback(
        self, completed, expected_revision: int, prior_write_safe: bool
    ) -> tuple[dict, int] | None:
        if self._pending_writeback is completed:
            self._pending_writeback = None
        deferred = self._deferred_writeback
        self._deferred_writeback = None
        if (
            not prior_write_safe
            or deferred is None
            or deferred[1] != expected_revision
            or self._configuration_revision
            not in (expected_revision, expected_revision + 1)
        ):
            return None
        return deferred[0], self._configuration_revision

    def _adopt_persisted_write_if_matching(
        self,
        current: dict | None,
        bundle: dict,
        expected_revision: int,
        lock_already_held: bool = False,
    ) -> bool:
        if (
            current is None
            or current.get("configuration_revision") != expected_revision + 1
            or current.get("access_token") != bundle.get("access_token")
            or current.get("refresh_token") != bundle.get("refresh_token")
        ):
            return False
        def adopt() -> bool:
            if self._configuration_revision != expected_revision:
                return False
            self._bundle = current
            self._configuration_revision = expected_revision + 1
            return True

        if lock_already_held:
            return adopt()
        with self._lock:
            return adopt()

    def _advance_if_current(self, bundle: dict, expected_revision: int) -> bool:
        with self._lock:
            if self._configuration_revision != expected_revision:
                return False
            self._bundle = bundle
            self._configuration_revision = expected_revision + 1
            return True


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
        client_secret: str = "",
        service_account_subject: str = "",
        tls_ca: str | None = None,
        tls_verify: bool = True,
        configuration_revision: int = 0,
    ) -> OidcGatewayAuth:
        with self._lock:
            existing = self._instances.get(workspace_id)
            if existing is not None:
                # The check and any reseed/replacement share the lock protecting
                # the current bundle and revision. A refresh/write-back cannot
                # rotate credentials between validation and publication.
                with existing._lock:
                    if configuration_revision < existing._configuration_revision:
                        raise StaleGatewayConfigurationError(
                            "Workspace gateway configuration snapshot was superseded"
                        )
                    if (
                        existing._issuer == issuer.rstrip("/")
                        and existing._client_id == client_id
                        and existing._audience == audience
                        and existing._tls_verify == tls_verify
                    ):
                        if refresh_token or client_secret:
                            existing._seed_unlocked(
                                refresh_token,
                                access_token,
                                expires_at,
                                client_secret,
                                service_account_subject,
                            )
                        existing._configuration_revision = configuration_revision
                        return existing
                    existing.close()

            auth = OidcGatewayAuth(
                issuer=issuer,
                client_id=client_id,
                audience=audience,
                workspace_id=workspace_id,
                client_secret=client_secret,
                service_account_subject=service_account_subject,
                tls_ca=tls_ca,
                tls_verify=tls_verify,
                configuration_revision=configuration_revision,
            )
            with contextlib.suppress(RuntimeError):
                auth.set_event_loop(asyncio.get_running_loop())
            if refresh_token or client_secret:
                auth.seed(
                    refresh_token=refresh_token,
                    access_token=access_token,
                    expires_at=expires_at,
                    client_secret=client_secret,
                    service_account_subject=service_account_subject,
                )
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
        if gw is None or (not gw.refresh_token and not gw.client_secret):
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
            "client_secret": gw.client_secret,
            "service_account_subject": gw.service_account_subject,
            "configuration_revision": gw.configuration_revision,
        }
    return None


async def _persist_workspace_bundle(
    workspace_id: int | None,
    bundle: dict,
    expected_revision: int,
) -> bool:
    if workspace_id is None:
        return False

    from sqlalchemy import update
    from swarmer.crypto import encrypt
    from swarmer.database import get_db
    from swarmer.models.workspace_gateway import WorkspaceGateway

    async for db in get_db():
        values: dict[str, Any] = {
            "access_token_enc": encrypt(bundle.get("access_token", ""))
            if bundle.get("access_token")
            else None,
            "access_token_expires_at": (
                datetime.fromtimestamp(bundle["expires_at"], tz=timezone.utc).replace(tzinfo=None)
                if bundle.get("expires_at")
                else None
            ),
            "configuration_revision": expected_revision + 1,
        }
        if bundle.get("refresh_token"):
            values["refresh_token_enc"] = encrypt(bundle["refresh_token"])
        result = await db.execute(
            update(WorkspaceGateway)
            .where(
                WorkspaceGateway.workspace_id == workspace_id,
                WorkspaceGateway.configuration_revision == expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1
    return False
