"""Tests for the redirect loop fix when a K8s token expires.

When a session cookie has authenticated=True but the underlying K8s bearer
token has expired, the app must NOT enter an infinite redirect loop
(ERR_TOO_MANY_REDIRECTS).

Root cause: not_authenticated_handler was redirecting to /login without
clearing the session, so /login saw authenticated=True and redirected back
to /workspaces, creating an infinite loop.

Fix: not_authenticated_handler clears the session before redirecting.

These tests use a self-contained mini-app that mirrors the relevant parts of
the real app (SessionMiddleware + NotAuthenticated handler + /login route)
without requiring K8s, the DB, or the full lifespan.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from swarmer.deps import NotAuthenticated


# ---------------------------------------------------------------------------
# Build a minimal test app that replicates the auth handler behaviour
# ---------------------------------------------------------------------------


def _make_test_app(clear_session_on_not_auth: bool) -> FastAPI:
    """Create a minimal FastAPI app for auth-handler testing.

    Parameters
    ----------
    clear_session_on_not_auth:
        If True, the NotAuthenticated handler calls request.session.clear()
        before redirecting (the fixed behaviour).
        If False, it does NOT clear the session (the buggy behaviour).
    """
    test_app = FastAPI()
    test_app.add_middleware(
        SessionMiddleware,
        secret_key="swarmer-test-secret-key-32bytes!",  # exactly 32 chars
        session_cookie="swarmer_session",
        same_site="lax",
        https_only=False,
    )

    @test_app.exception_handler(NotAuthenticated)
    async def _not_authenticated_handler(request: Request, exc: NotAuthenticated):
        if clear_session_on_not_auth:
            request.session.clear()
        return RedirectResponse(url="/login", status_code=302)

    @test_app.get("/login", response_class=HTMLResponse)
    async def _login_page(request: Request):
        if request.session.get("authenticated"):
            return RedirectResponse("/workspaces", status_code=303)
        return HTMLResponse("<html><body>login</body></html>", status_code=200)

    @test_app.get("/workspaces", response_class=HTMLResponse)
    async def _workspaces(request: Request):
        # Simulate expired token: always raise NotAuthenticated
        raise NotAuthenticated()

    @test_app.get("/_seed_auth")
    async def _seed_auth(request: Request):
        """Seed session with authenticated=True for testing."""
        request.session["authenticated"] = True
        request.session["username"] = "alice"
        return JSONResponse({"ok": True})

    @test_app.get("/_trigger_not_auth")
    async def _trigger_not_auth(request: Request):
        """Seed session then immediately raise NotAuthenticated."""
        request.session["authenticated"] = True
        raise NotAuthenticated()

    return test_app


# ---------------------------------------------------------------------------
# Fixture: shared pytest_asyncio fixture (matches project convention)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fixed_app():
    """App with the session-clearing fix applied."""
    return _make_test_app(clear_session_on_not_auth=True)


@pytest_asyncio.fixture
async def buggy_app():
    """App WITHOUT the fix — reproduces the redirect loop."""
    return _make_test_app(clear_session_on_not_auth=False)


# ===========================================================================
# Test: the buggy behaviour (documents the problem)
# ===========================================================================


class TestBuggyBehaviour:
    @pytest.mark.asyncio
    async def test_redirect_loop_without_fix(self, buggy_app):
        """Without the fix, /login redirects back to /workspaces if session has authenticated=True.

        This documents the root cause of ERR_TOO_MANY_REDIRECTS.
        """
        transport = httpx.ASGITransport(app=buggy_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Seed the session with authenticated=True
            r1 = await client.get("/_seed_auth")
            assert r1.status_code == 200

            # Trigger NotAuthenticated (simulates expired token)
            r2 = await client.get("/_trigger_not_auth", follow_redirects=False)
            assert r2.status_code == 302
            assert r2.headers["location"] == "/login"

            # WITHOUT the fix: /login sees authenticated=True and loops back
            r3 = await client.get("/login", follow_redirects=False)
            # This would be 303 (redirect to /workspaces) — the loop trigger
            assert r3.status_code == 303, (
                "Buggy app should redirect back to /workspaces, demonstrating the loop"
            )
            assert "/workspaces" in r3.headers["location"]


# ===========================================================================
# Test: the fix prevents the redirect loop
# ===========================================================================


class TestRedirectLoopFix:
    @pytest.mark.asyncio
    async def test_session_cleared_before_redirect_to_login(self, fixed_app):
        """After fix: /login returns 200 (login page) when token expires.

        The not_authenticated_handler clears the session before redirecting,
        so /login no longer sees authenticated=True and does not loop.
        """
        transport = httpx.ASGITransport(app=fixed_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Seed the session with authenticated=True
            r1 = await client.get("/_seed_auth")
            assert r1.status_code == 200

            # Trigger NotAuthenticated (simulates expired K8s token)
            r2 = await client.get("/_trigger_not_auth", follow_redirects=False)
            assert r2.status_code == 302
            assert r2.headers["location"] == "/login"

            # WITH the fix: session is cleared, /login renders the login page (200)
            r3 = await client.get("/login", follow_redirects=False)
            assert r3.status_code == 200, (
                f"Expected login page (200) but got {r3.status_code} — "
                "session was not cleared properly, redirect loop would occur"
            )

    @pytest.mark.asyncio
    async def test_expired_token_on_protected_route_clears_session(self, fixed_app):
        """Expired token: protected route → 302 /login → 200 login page (no loop).

        This is the end-to-end test of the full redirect chain with the fix.
        """
        transport = httpx.ASGITransport(app=fixed_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Seed the session first
            await client.get("/_seed_auth")

            # Hit protected /workspaces — always raises NotAuthenticated in this test app
            r2 = await client.get("/workspaces", follow_redirects=False)
            assert r2.status_code == 302
            assert r2.headers["location"] == "/login"

            # Follow the redirect — must get the login page, not another redirect
            r3 = await client.get("/login", follow_redirects=False)
            assert r3.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_session_still_redirects_to_workspaces(self, fixed_app):
        """Sanity: /login with a valid authenticated session still redirects to /workspaces.

        Verifies the happy path (user is actually logged in) is not broken.
        """
        transport = httpx.ASGITransport(app=fixed_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Seed an authenticated session
            r1 = await client.get("/_seed_auth")
            assert r1.status_code == 200

            # /login with a valid session → 303 to /workspaces
            r2 = await client.get("/login", follow_redirects=False)
            assert r2.status_code == 303
            assert "/workspaces" in r2.headers["location"]

    @pytest.mark.asyncio
    async def test_unauthenticated_request_gets_login_page(self, fixed_app):
        """Unauthenticated user hits /workspaces → redirected to /login (200)."""
        transport = httpx.ASGITransport(app=fixed_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # No session cookie at all
            r1 = await client.get("/workspaces", follow_redirects=False)
            assert r1.status_code == 302
            assert r1.headers["location"] == "/login"

            r2 = await client.get("/login", follow_redirects=False)
            assert r2.status_code == 200
