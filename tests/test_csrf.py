"""Unit tests for CSRF token helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from starlette.requests import Request

from swarmer.csrf import CSRFError, ensure_csrf_token, validate_csrf_token


def _request(session: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "session": session or {},
    }
    return Request(scope)


class TestCSRF:
    def test_ensure_csrf_token_creates_and_reuses(self):
        request = _request()
        token = ensure_csrf_token(request)
        assert token
        assert request.session["csrf_token"] == token
        assert ensure_csrf_token(request) == token

    def test_validate_csrf_token_accepts_matching_token(self):
        request = _request({"csrf_token": "abc123"})
        validate_csrf_token(request, "abc123")

    def test_validate_csrf_token_rejects_missing(self):
        request = _request({"csrf_token": "abc123"})
        with pytest.raises(CSRFError):
            validate_csrf_token(request, "")

    def test_validate_csrf_token_rejects_mismatch(self):
        request = _request({"csrf_token": "abc123"})
        with pytest.raises(CSRFError):
            validate_csrf_token(request, "wrong")
