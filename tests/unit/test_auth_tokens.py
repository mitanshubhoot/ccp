"""MT4 (A-129): who is asking is decided by a verified token, never by a typed name.

The defect: every decision recorded a reviewer as whatever name was typed (`--reviewer`),
so nothing said that a real, signed-in person had made it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ccp import auth
from ccp.auth import dev_issuer
from ccp.auth.tokens import InvalidToken, StaticKeys, Verifier
from ccp.config import Settings

NOW = datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(ccp_data_dir=tmp_path, ccp_auth_mode="dev")


def _claims(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": dev_issuer.DEV_ISSUER,
        "sub": "user-1",
        "aud": "ccp",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
        "email": "ana@acme.example",
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


def _signed(settings: Settings, claims: dict[str, Any], key: Any = None, **header: Any) -> str:
    kid = dev_issuer.jwks(settings)["keys"][0]["kid"]
    return jwt.encode(
        claims,
        key or dev_issuer.private_key(settings),
        algorithm=header.pop("algorithm", "RS256"),
        headers={"kid": kid, **header},
    )


def test_a_dev_token_proves_its_principal(settings: Settings) -> None:
    token = dev_issuer.mint(settings, "user-1", "ana@acme.example", "Ana")
    who = auth.authenticate(settings, token)
    assert (who.issuer, who.subject, who.email, who.name) == (
        dev_issuer.DEV_ISSUER,
        "user-1",
        "ana@acme.example",
        "Ana",
    )
    assert who.principal_id == f"{dev_issuer.DEV_ISSUER}#user-1"


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        (_claims(exp=int((NOW - timedelta(hours=2)).timestamp())), "expired"),
        (_claims(aud="another-client"), "another audience"),
        (_claims(iss="https://someone-else.example"), "issuer"),
        (_claims(sub=None), "sub"),
    ],
)
def test_a_token_that_is_not_for_us_is_refused(
    settings: Settings, claims: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidToken, match=message):
        auth.authenticate(settings, _signed(settings, claims))


def test_a_token_signed_by_another_key_is_refused(settings: Settings) -> None:
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken, match="Signature"):
        auth.authenticate(settings, _signed(settings, _claims(), key=stranger))


def test_the_public_key_cannot_sign_a_shared_secret_token(settings: Settings) -> None:
    """Algorithm confusion: an HS256 token keyed with the published key, or no algorithm."""
    public_pem = str(dev_issuer.jwks(settings))
    forged = jwt.encode(_claims(), public_pem, algorithm="HS256", headers={"kid": "x"})
    with pytest.raises(InvalidToken, match="not accepted"):
        auth.authenticate(settings, forged)
    unsigned = jwt.encode(_claims(), None, algorithm="none")
    with pytest.raises(InvalidToken, match="not accepted"):
        auth.authenticate(settings, unsigned)
    with pytest.raises(InvalidToken, match="not a token"):
        auth.authenticate(settings, "not-a-token")


def test_a_cognito_access_token_names_the_client_in_client_id(settings: Settings) -> None:
    access = _claims(aud=None, token_use="access", client_id="ccp")
    who = auth.authenticate(settings, _signed(settings, access))
    assert who.subject == "user-1"
    other = _claims(aud=None, token_use="access", client_id="another-client")
    with pytest.raises(InvalidToken, match="another client"):
        auth.authenticate(settings, _signed(settings, other))


def test_an_oidc_server_never_trusts_the_dev_issuer(settings: Settings) -> None:
    token = dev_issuer.mint(settings, "user-1")
    jwks = dev_issuer.jwks(settings)
    production = Verifier("https://cognito-idp.example/pool", "ccp", StaticKeys(jwks))
    with pytest.raises(InvalidToken, match="issuer"):
        production.verify(token)
    oidc = settings.model_copy(update={"ccp_auth_mode": "oidc", "ccp_oidc_issuer": None})
    with pytest.raises(RuntimeError, match="CCP_OIDC_ISSUER"):
        auth.verifier(oidc)
    configured = settings.model_copy(
        update={"ccp_auth_mode": "oidc", "ccp_oidc_issuer": "https://cognito-idp.example/pool/"}
    )
    assert auth.verifier(configured).issuer == "https://cognito-idp.example/pool"
