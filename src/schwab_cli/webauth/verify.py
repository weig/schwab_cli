"""JWT verification against the configured providers.

Flow per token:

1. Parse the (unverified) claims to read ``iss`` and route to the
   matching :class:`~schwab_cli.webauth.config.ProviderConfig` —
   unknown issuer → :class:`UnknownIssuer`.
2. Resolve the signing key (injectable; production uses
   :class:`JwksKeyResolver` — cached JWKS with refetch-on-unknown-kid
   for key rotation).
3. ``jwt.decode``: signature + exact ``iss`` + required ``aud`` +
   ``exp``/``nbf`` with the provider's clock skew.
4. Subject gate: ``sub`` then ``email`` against the provider allowlist
   (default closed) → :class:`SubjectNotAllowed`.
5. Scopes = token scopes (``scope_claim``: space-delimited string or
   JSON array) ∪ statically granted ones.

Token values never appear in exceptions, logs, or notifications.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import httpx
import jwt

if TYPE_CHECKING:
    from schwab_cli.webauth.config import ProviderConfig


class WebAuthError(Exception):
    """Base class for token rejections."""


class UnknownIssuer(WebAuthError):
    """Token's ``iss`` does not match any configured provider."""


class InvalidToken(WebAuthError):
    """Malformed, badly signed, expired, or wrong-audience token."""


class SubjectNotAllowed(WebAuthError):
    """Valid token, but the subject is not in the provider allowlist."""


@dataclass(frozen=True)
class Principal:
    """Authenticated caller attached to a request."""

    provider: str
    subject: str
    email: str | None
    scopes: frozenset[str]


KeyResolver = Callable[["ProviderConfig", str], object]


class TokenVerifier:
    """Validates bearer JWTs against a set of providers (keyed by iss)."""

    def __init__(
        self,
        providers: Iterable["ProviderConfig"],
        *,
        key_resolver: KeyResolver | None = None,
    ) -> None:
        self._by_issuer: dict[str, "ProviderConfig"] = {
            p.issuer: p for p in providers
        }
        self._key_resolver = key_resolver or JwksKeyResolver()

    def verify(self, token: str) -> Principal:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise InvalidToken(f"malformed token: {type(e).__name__}") from e

        issuer = unverified.get("iss")
        provider = self._by_issuer.get(issuer)
        if provider is None:
            raise UnknownIssuer(f"no provider configured for issuer {issuer!r}")

        key = self._key_resolver(provider, token)

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(provider.algorithms),
                audience=provider.audience,
                issuer=provider.issuer,
                leeway=provider.clock_skew_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise InvalidToken(f"token rejected: {type(e).__name__}") from e

        subject = claims.get("sub", "")
        email = claims.get("email")
        static = self._static_scopes(provider, subject=subject, email=email)
        token_scopes = _parse_scope_claim(claims.get(provider.scope_claim))
        return Principal(
            provider=provider.name,
            subject=subject,
            email=email,
            scopes=frozenset(token_scopes) | static,
        )

    @staticmethod
    def _static_scopes(
        provider: "ProviderConfig", *, subject: str, email: str | None,
    ) -> frozenset[str]:
        if provider.allow_all_subjects:
            return provider.wildcard_scopes
        if subject and subject in provider.subject_scopes:
            return provider.subject_scopes[subject]
        if email and email in provider.subject_scopes:
            return provider.subject_scopes[email]
        raise SubjectNotAllowed(
            f"subject not in {provider.name!r} allowlist"
        )


def _parse_scope_claim(value) -> set[str]:
    """RFC 6749 space-delimited string, or a JSON array (Auth0
    ``permissions`` / Azure ``scp``-style). Anything else → no scopes."""
    if isinstance(value, str):
        return {s for s in value.split() if s}
    if isinstance(value, list):
        return {s for s in value if isinstance(s, str) and s}
    return set()


class JwksKeyResolver:
    """Production key resolver: OIDC discovery + cached JWKS.

    * ``jwks_uri`` from the provider config, else from
      ``{issuer}/.well-known/openid-configuration`` (cached forever per
      process — it never changes in practice).
    * JWKS document cached per provider; an unknown ``kid`` forces ONE
      refetch (covers provider key rotation) before giving up.
    * Any transport failure surfaces as :class:`InvalidToken` — the
      request fails closed.
    """

    def __init__(self, *, timeout_s: float = 5.0) -> None:
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._jwks_uri: dict[str, str] = {}       # issuer -> jwks uri
        self._keys: dict[str, dict[str, object]] = {}  # issuer -> kid -> key

    def __call__(self, provider: "ProviderConfig", token: str) -> object:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise InvalidToken(f"malformed token header: {type(e).__name__}") from e
        if not kid:
            raise InvalidToken("token header has no kid")

        with self._lock:
            cached = self._keys.get(provider.issuer, {}).get(kid)
        if cached is not None:
            return cached

        # Cache miss: fetch, and on an absent kid retry ONCE — a key
        # rotation can leave the first response (stale CDN edge) without
        # the new key. Two strikes → reject. Each pass rechecks the
        # shared cache first so a thundering herd of requests after a
        # rotation collapses onto whichever thread fetched first.
        for _attempt in (1, 2):
            with self._lock:
                cached = self._keys.get(provider.issuer, {}).get(kid)
            if cached is not None:
                return cached
            keys = self._fetch_jwks(provider)
            with self._lock:
                self._keys[provider.issuer] = keys
            key = keys.get(kid)
            if key is not None:
                return key
        raise InvalidToken("token kid not present in provider JWKS")

    def _fetch_jwks(self, provider: "ProviderConfig") -> dict[str, object]:
        uri = provider.jwks_uri or self._discover_jwks_uri(provider)
        try:
            resp = httpx.get(uri, timeout=self._timeout_s)
            resp.raise_for_status()
            doc = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise InvalidToken(
                f"JWKS fetch failed for {provider.name!r}: {type(e).__name__}"
            ) from e
        # Fail closed on structurally bogus documents (null / array /
        # string bodies) instead of letting an AttributeError escape.
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise InvalidToken(
                f"JWKS for {provider.name!r} is not a valid key-set document"
            )
        keys: dict[str, object] = {}
        for jwk_dict in doc["keys"]:
            if not isinstance(jwk_dict, dict):
                continue
            kid = jwk_dict.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(jwk_dict).key
            except jwt.PyJWTError:
                continue  # skip unusable entries; others may still serve
        return keys

    def _discover_jwks_uri(self, provider: "ProviderConfig") -> str:
        with self._lock:
            cached = self._jwks_uri.get(provider.issuer)
        if cached is not None:
            return cached
        discovery = (
            provider.issuer.rstrip("/")
            + "/.well-known/openid-configuration"
        )
        try:
            resp = httpx.get(discovery, timeout=self._timeout_s)
            resp.raise_for_status()
            doc = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise InvalidToken(
                f"OIDC discovery failed for {provider.name!r}: {type(e).__name__}"
            ) from e
        uri = doc.get("jwks_uri") if isinstance(doc, dict) else None
        if not isinstance(uri, str) or not uri.startswith("https://"):
            raise InvalidToken(
                f"OIDC discovery for {provider.name!r} returned no usable jwks_uri"
            )
        with self._lock:
            self._jwks_uri[provider.issuer] = uri
        return uri
