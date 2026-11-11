"""A local identity provider for development and tests (spec.md A-129).

It stands in for the customer's provider behind Cognito so every authenticated path runs
offline: one RSA key in the data directory, its public half as a JWKS document, and tokens
minted with it under the issuer `DEV_ISSUER`. A server in `oidc` mode never trusts it: its
verifier knows only the configured issuer and that issuer's published keys.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ccp.config import Settings

DEV_ISSUER = "https://dev-issuer.ccp.invalid"


def _key_path(settings: Settings) -> Path:
    return settings.ccp_data_dir / "dev-issuer" / "signing-key.pem"


def private_key(settings: Settings) -> rsa.RSAPrivateKey:
    """The dev issuer's key, made on first use."""
    path = _key_path(settings)
    if path.is_file():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(loaded, rsa.RSAPrivateKey)
        return loaded
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def jwks(settings: Settings) -> dict[str, Any]:
    """The public half, as a JWKS document, with its key id."""
    public = private_key(settings).public_key()
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(public))
    kid = hashlib.sha256(json.dumps(jwk, sort_keys=True).encode()).hexdigest()[:16]
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def mint(
    settings: Settings,
    subject: str,
    email: str | None = None,
    name: str | None = None,
    ttl: timedelta = timedelta(hours=8),
    audience: str | None = None,
    now: datetime | None = None,
) -> str:
    """An ID token from the dev issuer."""
    moment = now or datetime.now(UTC)
    kid = jwks(settings)["keys"][0]["kid"]
    claims = {
        "iss": DEV_ISSUER,
        "sub": subject,
        "aud": audience or settings.ccp_oidc_audience,
        "iat": int(moment.timestamp()),
        "exp": int((moment + ttl).timestamp()),
        "token_use": "id",
        **({"email": email} if email else {}),
        **({"name": name} if name else {}),
    }
    return jwt.encode(claims, private_key(settings), algorithm="RS256", headers={"kid": kid})
