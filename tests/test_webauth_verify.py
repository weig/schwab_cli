"""Spec tests for schwab_cli.webauth.verify + scopes — JWT validation.

Fully offline: an RSA keypair is generated in-test, tokens are
self-signed with PyJWT, and the verifier gets an injected key resolver.
The HTTP JWKS resolver is exercised separately via respx.
"""
from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from schwab_cli.webauth.config import ProviderConfig
from schwab_cli.webauth.scopes import scope_satisfied
from schwab_cli.webauth.verify import (
    InvalidToken,
    JwksKeyResolver,
    Principal,
    SubjectNotAllowed,
    TokenVerifier,
    UnknownIssuer,
)

_NOW = int(time.time())
_ISS = "https://tenant.us.auth0.com/"
_AUD = "https://schwab-api.local"


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, pem


def _provider(**over) -> ProviderConfig:
    base = dict(
        name="auth0",
        issuer=_ISS,
        audience=_AUD,
        algorithms=("RS256",),
        jwks_uri=None,
        scope_claim="scope",
        allow_all_subjects=False,
        subject_scopes={"auth0|abc": frozenset()},
        wildcard_scopes=frozenset(),
        clock_skew_s=60,
    )
    base.update(over)
    return ProviderConfig(**base)


def _token(pem, **over) -> str:
    claims = {
        "iss": _ISS,
        "aud": _AUD,
        "sub": "auth0|abc",
        "exp": _NOW + 600,
        "iat": _NOW,
        "scope": "marketdata accounts",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})


def _verifier(rsa_key, *providers) -> TokenVerifier:
    key, _pem = rsa_key
    public = key.public_key()
    return TokenVerifier(
        providers, key_resolver=lambda provider, token: public,
    )


# ---------------------------------------------------------------------------
# Verification core
# ---------------------------------------------------------------------------


def test_valid_token_yields_principal(rsa_key):
    v = _verifier(rsa_key, _provider())
    p = v.verify(_token(rsa_key[1]))
    assert isinstance(p, Principal)
    assert p.provider == "auth0"
    assert p.subject == "auth0|abc"
    assert p.scopes == frozenset({"marketdata", "accounts"})


def test_unknown_issuer_rejected(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(UnknownIssuer):
        v.verify(_token(rsa_key[1], iss="https://evil.example/"))


def test_wrong_audience_rejected(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(InvalidToken):
        v.verify(_token(rsa_key[1], aud="https://other-api"))


def test_expired_token_rejected(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(InvalidToken):
        v.verify(_token(rsa_key[1], exp=_NOW - 3600))


def test_garbage_token_rejected(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(InvalidToken):
        v.verify("not.a.jwt")


def test_bad_signature_rejected(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    v = _verifier(rsa_key, _provider())
    with pytest.raises(InvalidToken):
        v.verify(_token(other_pem))


def test_subject_not_in_allowlist_rejected(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(SubjectNotAllowed):
        v.verify(_token(rsa_key[1], sub="auth0|stranger"))


def test_email_matching_for_google_style_tokens(rsa_key):
    v = _verifier(rsa_key, _provider(
        subject_scopes={"me@gmail.com": frozenset({"accounts"})},
    ))
    p = v.verify(_token(
        rsa_key[1], sub="1234567890", email="me@gmail.com", scope=None,
    ))
    assert p.email == "me@gmail.com"
    # static grant supplies the scopes a Google ID token cannot carry
    assert p.scopes == frozenset({"accounts"})


def test_static_scopes_union_with_token_scopes(rsa_key):
    v = _verifier(rsa_key, _provider(
        subject_scopes={"auth0|abc": frozenset({"dataset"})},
    ))
    p = v.verify(_token(rsa_key[1]))
    assert p.scopes == frozenset({"marketdata", "accounts", "dataset"})


def test_allow_all_subjects_with_wildcard_static_scopes(rsa_key):
    v = _verifier(rsa_key, _provider(
        allow_all_subjects=True,
        subject_scopes={},
        wildcard_scopes=frozenset({"marketdata"}),
    ))
    p = v.verify(_token(rsa_key[1], sub="anyone-at-all", scope=None))
    assert p.scopes == frozenset({"marketdata"})


def test_scope_claim_as_list_permissions_style(rsa_key):
    # Auth0 RBAC puts permissions in a JSON array claim.
    v = _verifier(rsa_key, _provider(scope_claim="permissions"))
    p = v.verify(_token(
        rsa_key[1], scope=None, permissions=["accounts", "order:default"],
    ))
    assert p.scopes == frozenset({"accounts", "order:default"})


def test_multiple_providers_routed_by_issuer(rsa_key):
    other = _provider(
        name="google",
        issuer="https://accounts.google.com",
        subject_scopes={"me@gmail.com": frozenset({"dataset"})},
    )
    v = _verifier(rsa_key, _provider(), other)
    p = v.verify(_token(
        rsa_key[1], iss="https://accounts.google.com",
        sub="999", email="me@gmail.com", scope=None,
    ))
    assert p.provider == "google"
    assert p.scopes == frozenset({"dataset"})


# ---------------------------------------------------------------------------
# Scope checking
# ---------------------------------------------------------------------------


def test_scope_exact_match():
    assert scope_satisfied(frozenset({"accounts"}), "accounts")
    assert not scope_satisfied(frozenset({"accounts"}), "positions")


def test_scope_order_wildcard_grant():
    granted = frozenset({"order:*"})
    assert scope_satisfied(granted, "order:default")
    assert scope_satisfied(granted, "order:conservative")
    assert not scope_satisfied(granted, "accounts")


def test_scope_specific_order_profile():
    granted = frozenset({"order:conservative"})
    assert scope_satisfied(granted, "order:conservative")
    assert not scope_satisfied(granted, "order:default")


# ---------------------------------------------------------------------------
# HTTP JWKS resolver (respx, offline)
# ---------------------------------------------------------------------------


def _jwk_payload(key) -> dict:
    from jwt.algorithms import RSAAlgorithm
    import json as _json

    jwk = _json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


@respx.mock
def test_jwks_resolver_discovers_and_resolves(rsa_key):
    key, pem = rsa_key
    respx.get(f"{_ISS}.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json={
            "jwks_uri": f"{_ISS}jwks.json",
        }),
    )
    respx.get(f"{_ISS}jwks.json").mock(
        return_value=httpx.Response(200, json=_jwk_payload(key)),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier((_provider(),), key_resolver=resolver)
    p = v.verify(_token(pem))
    assert p.subject == "auth0|abc"


@respx.mock
def test_jwks_resolver_caches_across_calls(rsa_key):
    key, pem = rsa_key
    disc = respx.get(f"{_ISS}.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json={"jwks_uri": f"{_ISS}jwks.json"}),
    )
    jwks = respx.get(f"{_ISS}jwks.json").mock(
        return_value=httpx.Response(200, json=_jwk_payload(key)),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier((_provider(),), key_resolver=resolver)
    v.verify(_token(pem))
    v.verify(_token(pem))
    assert disc.call_count == 1
    assert jwks.call_count == 1


@respx.mock
def test_jwks_resolver_uses_explicit_jwks_uri_without_discovery(rsa_key):
    key, pem = rsa_key
    jwks = respx.get("https://keys.example/jwks").mock(
        return_value=httpx.Response(200, json=_jwk_payload(key)),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier(
        (_provider(jwks_uri="https://keys.example/jwks"),),
        key_resolver=resolver,
    )
    v.verify(_token(pem))
    assert jwks.called


@respx.mock
def test_jwks_resolver_unknown_kid_refetches_once(rsa_key):
    key, pem = rsa_key
    payload_without = {"keys": []}
    payload_with = _jwk_payload(key)
    jwks = respx.get("https://keys.example/jwks").mock(
        side_effect=[
            httpx.Response(200, json=payload_without),
            httpx.Response(200, json=payload_with),
        ],
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier(
        (_provider(jwks_uri="https://keys.example/jwks"),),
        key_resolver=resolver,
    )
    # First resolution: kid missing → forced refetch → found (key rotation).
    p = v.verify(_token(pem))
    assert p.subject == "auth0|abc"
    assert jwks.call_count == 2


@respx.mock
def test_jwks_resolver_unreachable_is_invalid_token(rsa_key):
    _key, pem = rsa_key
    respx.get("https://keys.example/jwks").mock(
        side_effect=httpx.ConnectError("refused"),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier(
        (_provider(jwks_uri="https://keys.example/jwks"),),
        key_resolver=resolver,
    )
    with pytest.raises(InvalidToken):
        v.verify(_token(pem))


@respx.mock
@pytest.mark.parametrize("body", [None, "x", [1, 2], {"keys": None}])
def test_jwks_non_object_body_fails_closed(rsa_key, body):
    """A 200 with a structurally bogus JSON body must reject as
    InvalidToken — never escape as AttributeError (fail-closed
    contract; an escape would surface as a 500 from the HTTP layer)."""
    _key, pem = rsa_key
    respx.get("https://keys.example/jwks").mock(
        return_value=httpx.Response(200, json=body),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier(
        (_provider(jwks_uri="https://keys.example/jwks"),),
        key_resolver=resolver,
    )
    with pytest.raises(InvalidToken):
        v.verify(_token(pem))


@respx.mock
def test_discovery_non_object_body_fails_closed(rsa_key):
    _key, pem = rsa_key
    respx.get(f"{_ISS}.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=None),
    )
    resolver = JwksKeyResolver()
    v = TokenVerifier((_provider(),), key_resolver=resolver)
    with pytest.raises(InvalidToken):
        v.verify(_token(pem))


def test_token_without_iss_is_unknown_issuer(rsa_key):
    v = _verifier(rsa_key, _provider())
    with pytest.raises(UnknownIssuer):
        v.verify(_token(rsa_key[1], iss=None))
