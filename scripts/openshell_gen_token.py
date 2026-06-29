"""
Generate a JWT bearer token for the OpenShell gateway.

Reads the Ed25519 signing key and KID from the 'openshell-jwt-keys' Kubernetes
Secret in the openshell namespace, then produces a token the gateway will accept.

Usage:
  python3 scripts/openshell_gen_token.py [--days 30] [--namespace openshell]
"""
import argparse
import base64
import json
import subprocess
import sys
import time

ISSUER = "openshell"
AUDIENCE = "openshell"


def _get_jwt_keys(namespace: str) -> tuple[str, str]:
    """Return (signing_pem_pkcs8, kid) from the cluster openshell-jwt-keys secret."""
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", "openshell-jwt-keys", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to read openshell-jwt-keys secret:\n{result.stderr}")
    secret_data = json.loads(result.stdout)["data"]
    raw_pem = base64.b64decode(secret_data["signing.pem"])
    kid = base64.b64decode(secret_data["kid"]).decode().strip()

    # The key is stored as OneAsymmetricKey (RFC 8410 v1) which may include the
    # optional public key field. PyJWT / cryptography expect standard PKCS8 (v0).
    # Re-export via Ed25519PrivateKey to get a clean 48-byte PKCS8 PEM.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            load_pem_private_key,
        )
    except ImportError:
        sys.exit("Missing dependency: pip install cryptography")

    try:
        key = load_pem_private_key(raw_pem, password=None)
    except ValueError:
        # OneAsymmetricKey with public key appended — extract raw private bytes manually.
        # PKCS8 header is 16 bytes for Ed25519: 30 xx 02 01 00/01 30 05 06 03 2b 65 70 04 22 04 20
        raw_der = base64.b64decode(
            b"\n".join(raw_pem.split(b"\n")[1:-2])  # strip PEM header/footer
        )
        # Private key scalar is always 32 bytes starting at byte 16
        priv_bytes = raw_der[16:48]
        key = Ed25519PrivateKey.from_private_bytes(priv_bytes)

    signing_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    return signing_pem, kid


def main():
    parser = argparse.ArgumentParser(description="Generate OpenShell JWT bearer token")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--namespace", default="openshell")
    args = parser.parse_args()

    try:
        import jwt
    except ImportError:
        sys.exit("Missing dependency: pip install pyjwt cryptography")

    signing_pem, kid = _get_jwt_keys(args.namespace)

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "swarmer",
        "iat": now,
        "exp": now + 86400 * args.days,
        "realm_access": {"roles": ["openshell-admin"]},
    }
    token = jwt.encode(payload, signing_pem, algorithm="EdDSA", headers={"kid": kid})
    print(token)


if __name__ == "__main__":
    main()
