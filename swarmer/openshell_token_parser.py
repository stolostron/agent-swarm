"""
Formatting helper and parser for OIDC refresh tokens, access tokens, and credential bundles.

Supports:
  1. Raw token strings (e.g. JWT format or opaque base64 string)
  2. JSON bundles (such as ~/.config/openshell/gateways/<name>/oidc_token.json)
  3. Key-value string representations (e.g. refresh_token=... or REFRESH_TOKEN: "...")
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ParsedTokenResult:
    refresh_token: str = ""
    access_token: str = ""
    expires_at: int | None = None
    issuer: str | None = None
    client_id: str | None = None
    format_detected: str = "raw"  # "raw", "json_bundle", "key_value"
    status: str = "valid"  # "valid", "empty", "malformed"
    message: str = ""
    char_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "format_detected": self.format_detected,
            "status": self.status,
            "message": self.message,
            "char_count": self.char_count,
        }


def parse_token_input(raw: str) -> ParsedTokenResult:
    """Parse and clean an OIDC token or credential bundle."""
    if not isinstance(raw, str):
        return ParsedTokenResult(
            status="malformed",
            message="Token input must be a string.",
        )

    if not raw or not raw.strip():
        return ParsedTokenResult(status="empty", message="No token provided.")

    cleaned = raw.strip()

    # 1. Try parsing as JSON bundle (e.g. oidc_token.json)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                has_refresh_key = "refresh_token" in data or "refreshToken" in data
                has_access_key = "access_token" in data or "accessToken" in data
                refresh = data.get("refresh_token") or data.get("refreshToken") or ""
                access = data.get("access_token") or data.get("accessToken") or ""
                if not isinstance(refresh, str):
                    refresh = ""
                if not isinstance(access, str):
                    access = ""
                expires_at = data.get("expires_at") or data.get("expiresAt")
                if expires_at is not None:
                    try:
                        expires_at = int(expires_at)
                    except (ValueError, TypeError):
                        expires_at = None

                issuer = data.get("issuer")
                issuer = issuer if isinstance(issuer, str) else None
                client_id = data.get("client_id") or data.get("clientId")
                client_id = client_id if isinstance(client_id, str) else None

                if refresh or access:
                    effective_token = refresh or access
                    return ParsedTokenResult(
                        refresh_token=refresh,
                        access_token=access,
                        expires_at=expires_at,
                        issuer=issuer,
                        client_id=client_id,
                        format_detected="json_bundle",
                        status="valid",
                        message=f"Extracted from JSON bundle ({len(effective_token)} chars).",
                        char_count=len(effective_token),
                    )

                if has_refresh_key or has_access_key:
                    return ParsedTokenResult(
                        format_detected="json_bundle",
                        status="malformed",
                        message=(
                            "JSON bundle did not include a string refresh_token or access_token."
                        ),
                    )
                return ParsedTokenResult(
                    format_detected="json_bundle",
                    status="malformed",
                    message="JSON bundle missing refresh_token/access_token.",
                )
        except json.JSONDecodeError:
            pass

    # 2. Try parsing key-value format (e.g. refresh_token=... or REFRESH_TOKEN: "...")
    kv_match = re.match(
        r"^(?:export\s+)?(?:REFRESH_TOKEN|refresh_token|TOKEN|token)\s*[:=]\s*[\"']?([^\"'\s]+)[\"']?$",
        cleaned,
        re.IGNORECASE,
    )
    if kv_match:
        token_val = kv_match.group(1).strip()
        return ParsedTokenResult(
            refresh_token=token_val,
            format_detected="key_value",
            status="valid",
            message=f"Extracted token from key-value assignment ({len(token_val)} chars).",
            char_count=len(token_val),
        )

    # 3. Handle raw token string (strip quotes, whitespace, or terminal escape artifacts)
    token_val = cleaned.strip("\"' \t\r\n")

    # Basic sanity check on token content
    if len(token_val) < 10:
        return ParsedTokenResult(
            refresh_token=token_val,
            format_detected="raw",
            status="malformed",
            message="Token string is unusually short (< 10 chars).",
            char_count=len(token_val),
        )

    return ParsedTokenResult(
        refresh_token=token_val,
        format_detected="raw",
        status="valid",
        message=f"Cleaned raw token ({len(token_val)} chars).",
        char_count=len(token_val),
    )
