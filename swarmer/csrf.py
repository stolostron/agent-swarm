"""CSRF token helpers for server-rendered HTML forms."""

import secrets

from starlette.requests import Request


class CSRFError(Exception):
    """Raised when a submitted CSRF token is missing or invalid."""


def ensure_csrf_token(request: Request) -> str:
    """Return the session CSRF token, creating one if needed."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf_token(request: Request, submitted: str) -> None:
    """Validate a form-submitted CSRF token against the session value."""
    expected = request.session.get("csrf_token", "")
    if not submitted or not expected or not secrets.compare_digest(submitted, expected):
        raise CSRFError("Invalid or missing CSRF token")
