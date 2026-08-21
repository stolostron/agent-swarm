from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None
_session_secret: str | None = None


def _load_or_create_key(key_file: str) -> bytes:
    env_val = os.environ.get("SWARMER_SECRET_KEY", "")
    if env_val:
        raw = base64.urlsafe_b64decode(env_val.encode())
        if len(raw) != 32:
            raise ValueError("SWARMER_SECRET_KEY must decode to exactly 32 bytes")
        return raw

    path = Path(key_file)
    if path.exists():
        raw = path.read_bytes().strip()
        return base64.urlsafe_b64decode(raw)

    raw = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.urlsafe_b64encode(raw))
    logger.warning("Generated new secret key at %s — existing encrypted secrets will be unreadable", key_file)
    return raw


def init_crypto(key_file: str) -> None:
    global _fernet, _session_secret
    raw = _load_or_create_key(key_file)
    fernet_key = base64.urlsafe_b64encode(raw)
    _fernet = Fernet(fernet_key)
    # session secret: HMAC-safe hex of raw key with prefix
    import hashlib
    _session_secret = hashlib.sha256(b"session:" + raw).hexdigest()


def derive_session_secret(key_file: str) -> str:
    if _session_secret is None:
        init_crypto(key_file)
    return _session_secret


def encrypt(plaintext: str) -> str:
    if _fernet is None:
        raise RuntimeError("crypto not initialized")
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if _fernet is None:
        raise RuntimeError("crypto not initialized")
    if not ciphertext:
        return ""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt value — key may have been rotated")
        return ""
