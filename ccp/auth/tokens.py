"""Who is asking: an OIDC token, verified on the server (spec.md A-129).

A person signs in at the customer's identity provider, federated through Cognito (MT4), and
CCP receives a signed token. `Verifier` checks it before anything else reads it: the
signature against the issuer's published keys, an accepted algorithm only (never `none`,
never a shared-secret algorithm a public key could be passed off as), the issuer, the
audience, and the expiry. An ID token names CCP's client in `aud`; a Cognito access token
names it in `client_id`. What comes out is a `Principal`: the issuer and the subject, which
together never change for a person, with an email and a name for display only. Roles are
CCP's own, per tenant (`members.py`), never read from the token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt

ACCEPTED_ALGORITHMS = ("RS256", "ES256")


class InvalidToken(PermissionError):
    """The token cannot be trusted; the request is refused."""


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    email: str | None = None
    name: str | None = None

    @property
    def principal_id(self) -> str:
        """Stable for one person at one identity provider."""
        return f"{self.issuer}#{self.subject}"

    @property
    def label(self) -> str:
        """For display and for the reviewer column of older tables."""
        return self.email or self.name or self.subject


class StaticKeys:
    """Keys from a JWKS document held in memory (the local dev issuer, tests)."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys = {k.key_id: k for k in jwt.PyJWKSet.from_dict(dict(jwks)).keys}

    def key_for(self, kid: str | None) -> Any:
        if kid is None or kid not in self._keys:
            raise InvalidToken(f"no published key {kid!r}")
        return self._keys[kid].key


class RemoteKeys:
    """Keys from the issuer's JWKS endpoint, cached (Cognito: {issuer}/.well-known/jwks.json)."""

    def __init__(self, url: str) -> None:
        self._client = jwt.PyJWKClient(url, cache_keys=True)

    def key_for(self, kid: str | None) -> Any:
        if kid is None:
            raise InvalidToken("the token names no key")
        try:
            return self._client.get_signing_key(kid).key
        except jwt.PyJWKClientError as exc:
            raise InvalidToken(f"no published key {kid!r}: {exc}") from exc


class Verifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        keys: StaticKeys | RemoteKeys,
        leeway_seconds: int = 60,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.keys = keys
        self.leeway = leeway_seconds

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"not a token: {exc}") from exc
        algorithm = header.get("alg")
        if algorithm not in ACCEPTED_ALGORITHMS:
            raise InvalidToken(f"algorithm {algorithm!r} is not accepted")
        key = self.keys.key_for(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=list(ACCEPTED_ALGORITHMS),
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        if claims.get("token_use") == "access":  # Cognito: the client is in client_id
            if claims.get("client_id") != self.audience:
                raise InvalidToken("the access token is for another client")
        else:
            audience = claims.get("aud")
            audiences = audience if isinstance(audience, list) else [audience]
            if self.audience not in audiences:
                raise InvalidToken("the token is for another audience")
        name = claims.get("name") or claims.get("cognito:username")
        email = claims.get("email")
        return Principal(
            issuer=self.issuer,
            subject=str(claims["sub"]),
            email=str(email) if email else None,
            name=str(name) if name else None,
        )
