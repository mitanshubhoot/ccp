"""Authentication and roles (spec.md A-129).

    tokens.py      an OIDC token verified on the server, to a Principal
    dev_issuer.py  a local identity provider for development and tests
    members.py     roles per tenant, CCP's own, never read from a token

`ccp_auth_mode` picks whom the server trusts: `dev` trusts only the local dev issuer;
`oidc` trusts only the configured issuer (Cognito, federated to each customer's SAML or
OIDC provider) and the keys it publishes.
"""

from __future__ import annotations

from ccp.auth.tokens import InvalidToken, Principal, RemoteKeys, StaticKeys, Verifier
from ccp.config import Settings

__all__ = ["InvalidToken", "Principal", "authenticate", "verifier"]


def verifier(settings: Settings) -> Verifier:
    if settings.ccp_auth_mode == "dev":
        from ccp.auth import dev_issuer

        keys = StaticKeys(dev_issuer.jwks(settings))
        return Verifier(dev_issuer.DEV_ISSUER, settings.ccp_oidc_audience, keys)
    if not settings.ccp_oidc_issuer:
        raise RuntimeError("CCP_AUTH_MODE is oidc but CCP_OIDC_ISSUER is not set")
    issuer = settings.ccp_oidc_issuer.rstrip("/")
    url = settings.ccp_oidc_jwks_url or f"{issuer}/.well-known/jwks.json"
    return Verifier(issuer, settings.ccp_oidc_audience, RemoteKeys(url))


def authenticate(settings: Settings, token: str) -> Principal:
    """The principal a token proves, or InvalidToken."""
    return verifier(settings).verify(token)
