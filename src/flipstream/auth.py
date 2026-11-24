"""Cloudflare Access JWT check.

Access signs every request it lets through with a JWT in the
Cf-Access-Jwt-Assertion header. Checking it here means a request that reaches
the app some other way (a tunnel misconfiguration, someone on the LAN) is
refused. Docs: https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/
"""

from collections.abc import Callable
from typing import Any

import jwt

HEADER = "Cf-Access-Jwt-Assertion"

KeyResolver = Callable[[str], Any]


class AccessDenied(Exception):
    """The request has no valid Access token."""


class AccessVerifier:
    """Checks the Access token on a request against Cloudflare's public keys."""

    def __init__(self, team_domain: str, aud: str, key_resolver: KeyResolver | None = None):
        """Set up the check for one Access application.

        aud is the application's audience tag. key_resolver returns the public key
        for a token; the default fetches Cloudflare's keys, the tests pass their own.
        """
        if not team_domain or not aud:
            raise ValueError("CF_TEAM_DOMAIN and CF_ACCESS_AUD must both be set")
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        if key_resolver is None:
            jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True)

            def key_resolver(token: str) -> Any:
                return jwks.get_signing_key_from_jwt(token).key

        self._resolve_key = key_resolver

    def verify(self, token: str | None) -> dict[str, Any]:
        """Return the token's claims (email and so on), or raise AccessDenied."""
        if not token:
            raise AccessDenied("missing Access token")
        try:
            key = self._resolve_key(token)
            return jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.aud,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(str(exc)) from exc
