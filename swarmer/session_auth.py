"""Short-lived credentials issued to the Agent Swarm MCP process."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

from swarmer.config import settings
from swarmer.crypto import derive_session_secret

_PREFIX = "as1."
_TTL_SECONDS = 3600
TOKEN_REFRESH_INTERVAL = 3000


def mint_session_token(session_id: int, workspace_id: int, ttl: int = _TTL_SECONDS) -> str:
    payload = {
        "sid": session_id,
        "wid": workspace_id,
        "exp": int(time.time()) + ttl,
    }
    encoded = _encode(payload)
    signature = _sign(encoded)
    return f"{_PREFIX}{encoded}.{signature}"


def validate_session_token(token: str) -> dict | None:
    if not token.startswith(_PREFIX):
        return None
    try:
        encoded, signature = token[len(_PREFIX):].split(".", 1)
        expected = _sign(encoded)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if int(payload["exp"]) <= int(time.time()):
            return None
        return {"session_id": int(payload["sid"]), "workspace_id": int(payload["wid"])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def start_token_refresh_loop(
    session_id: int,
    workspace_id: int,
    provider_name: str,
    client=None,
    refresh_interval: int = TOKEN_REFRESH_INTERVAL,
) -> None:
    """Refresh the session token stored in an OpenShell provider until cancelled."""
    from swarmer import openshell_client

    try:
        while True:
            await asyncio.sleep(refresh_interval)
            token = mint_session_token(session_id, workspace_id)
            await openshell_client.ensure_provider(
                provider_name,
                "agent-swarm",
                {},
                credentials={"AGENT_SWARM_API_TOKEN": token},
                client=client,
            )
    except asyncio.CancelledError:
        raise


def _encode(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")


def _sign(encoded: str) -> str:
    secret = derive_session_secret(settings.secret_key_file).encode()
    return hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
