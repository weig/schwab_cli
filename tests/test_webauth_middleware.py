"""Spec tests for schwab_cli.webauth.middleware — the two-tier gate.

Tier 1: ``/api/*`` — peer must be in ``web.allow`` AND carry a valid
JWT (when providers are configured). Tier 2: everything else
(``/admin``, ``/auth``, ``/mcp``, ``/health``) — loopback peers only,
regardless of the allowlist: a fat-fingered wide bind must never expose
the internal control plane.

The peer address reader is injectable so tests don't depend on
Starlette TestClient's synthetic client tuples.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from schwab_cli.webauth.middleware import WebAuthMiddleware, scope_denial
from schwab_cli.webauth.verify import (
    InvalidToken,
    Principal,
    SubjectNotAllowed,
    UnknownIssuer,
)


def _principal(scopes=("marketdata",)) -> Principal:
    return Principal(
        provider="auth0", subject="auth0|abc", email=None,
        scopes=frozenset(scopes),
    )


class _FakeVerifier:
    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.tokens: list[str] = []

    def verify(self, token: str):
        self.tokens.append(token)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def _echo(request):
    p = request.scope.get("state", {}).get("principal")
    return JSONResponse({
        "principal": p.subject if p else None,
        "scopes": sorted(p.scopes) if p else None,
    })


async def _guarded(request):
    denial = scope_denial(request, "accounts")
    if denial is not None:
        return denial
    return JSONResponse({"ok": True})


def _app(
    *,
    verifier,
    has_providers: bool = True,
    allow: tuple[str, ...] = ("127.0.0.1", "::1", "10.0.0.5"),
    peer: str = "127.0.0.1",
) -> TestClient:
    inner = Starlette(routes=[
        Route("/api/v1/echo", _echo),
        Route("/api/v1/guarded", _guarded),
        Route("/api/v1/health", _echo),
        Route("/admin/status", _echo),
        Route("/health", _echo),
    ])
    wrapped = WebAuthMiddleware(
        inner,
        verifier=verifier,
        has_providers=has_providers,
        allow=allow,
        peer_of=lambda scope: peer,
    )
    return TestClient(wrapped, raise_server_exceptions=True)


_AUTH = {"Authorization": "Bearer x.y.z"}


# ---------------------------------------------------------------------------
# Tier 1: /api/* with providers configured
# ---------------------------------------------------------------------------


def test_api_without_token_is_401_with_www_authenticate():
    client = _app(verifier=_FakeVerifier(_principal()))
    resp = client.get("/api/v1/echo")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


def test_api_with_valid_token_passes_and_attaches_principal():
    client = _app(verifier=_FakeVerifier(_principal()))
    resp = client.get("/api/v1/echo", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["principal"] == "auth0|abc"
    assert resp.json()["scopes"] == ["marketdata"]


def test_api_with_invalid_token_is_401():
    client = _app(verifier=_FakeVerifier(InvalidToken("bad")))
    assert client.get("/api/v1/echo", headers=_AUTH).status_code == 401


def test_api_with_unknown_issuer_is_401():
    client = _app(verifier=_FakeVerifier(UnknownIssuer("nope")))
    assert client.get("/api/v1/echo", headers=_AUTH).status_code == 401


def test_api_with_disallowed_subject_is_403():
    client = _app(verifier=_FakeVerifier(SubjectNotAllowed("who")))
    assert client.get("/api/v1/echo", headers=_AUTH).status_code == 403


def test_api_peer_not_in_allow_is_rejected_before_auth():
    verifier = _FakeVerifier(_principal())
    client = _app(verifier=verifier, peer="192.168.1.99")
    resp = client.get("/api/v1/echo", headers=_AUTH)
    assert resp.status_code == 403
    assert verifier.tokens == []  # never reached the verifier


def test_api_allowed_proxy_peer_passes():
    client = _app(verifier=_FakeVerifier(_principal()), peer="10.0.0.5")
    assert client.get("/api/v1/echo", headers=_AUTH).status_code == 200


def test_api_health_needs_no_token_but_respects_allowlist():
    client = _app(verifier=_FakeVerifier(InvalidToken("never called")))
    assert client.get("/api/v1/health").status_code == 200
    bad_peer = _app(
        verifier=_FakeVerifier(InvalidToken("x")), peer="192.168.1.99",
    )
    assert bad_peer.get("/api/v1/health").status_code == 403


# ---------------------------------------------------------------------------
# Tier 1 legacy: no providers configured
# ---------------------------------------------------------------------------


def test_api_without_providers_loopback_passes_unauthenticated():
    client = _app(verifier=None, has_providers=False)
    resp = client.get("/api/v1/echo")
    assert resp.status_code == 200
    assert resp.json()["principal"] is None


def test_api_without_providers_remote_peer_is_503():
    client = _app(verifier=None, has_providers=False, peer="10.0.0.5")
    resp = client.get("/api/v1/echo")
    assert resp.status_code == 503
    assert "webauth" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Tier 2: internal endpoints are loopback-only, allowlist irrelevant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin/status", "/health"])
def test_internal_loopback_passes(path):
    client = _app(verifier=_FakeVerifier(_principal()))
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("peer", ["10.0.0.5", "192.168.1.99"])
def test_internal_remote_peer_is_404_even_if_allowlisted(peer):
    """web.allow lets a proxy reach /api — it must NEVER open /admin."""
    client = _app(verifier=_FakeVerifier(_principal()), peer=peer)
    assert client.get("/admin/status").status_code == 404


# ---------------------------------------------------------------------------
# scope_denial (route-level requirement)
# ---------------------------------------------------------------------------


def test_scope_denial_blocks_missing_scope():
    client = _app(verifier=_FakeVerifier(_principal(("marketdata",))))
    resp = client.get("/api/v1/guarded", headers=_AUTH)
    assert resp.status_code == 403
    assert "accounts" in resp.json()["error"]


def test_scope_denial_allows_granted_scope():
    client = _app(verifier=_FakeVerifier(_principal(("accounts",))))
    assert client.get("/api/v1/guarded", headers=_AUTH).status_code == 200


def test_scope_denial_legacy_mode_allows():
    client = _app(verifier=None, has_providers=False)
    assert client.get("/api/v1/guarded").status_code == 200


# ---------------------------------------------------------------------------
# Raw-ASGI hardening cases (bypass httpx's client-side path normalization)
# ---------------------------------------------------------------------------


def _raw_asgi_status(
    *, path: str, peer: str, scope_type: str = "http",
    headers=(), verifier=None, has_providers: bool = True,
) -> object:
    """Drive the middleware directly with a hand-built ASGI scope and
    return the HTTP status (or websocket close code)."""
    import asyncio

    inner_called: list[str] = []

    async def inner(scope, receive, send):
        inner_called.append(scope["path"])
        if scope["type"] == "http":
            from starlette.responses import JSONResponse

            await JSONResponse({"inner": True})(scope, receive, send)
        else:
            await send({"type": "websocket.accept"})

    mw = WebAuthMiddleware(
        inner,
        verifier=verifier or _FakeVerifier(_principal()),
        has_providers=has_providers,
        allow=("10.0.0.5",),
        peer_of=lambda scope: peer,
    )
    scope = {
        "type": scope_type,
        "path": path,
        "headers": list(headers),
        "client": (peer, 12345),
        "method": "GET",
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        messages.append(message)

    asyncio.run(mw(scope, receive, send))
    if scope_type == "websocket":
        close = [m for m in messages if m["type"] == "websocket.close"]
        if close:
            return close[0]["code"]
        return "accepted" if inner_called else None
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"]


def test_path_traversal_into_internal_is_tier2_404():
    """/api/../admin/... must classify as tier 2: a remote scanner gets
    404, learning nothing — not a tier-1 401/403 that confirms the path."""
    status = _raw_asgi_status(path="/api/../admin/status", peer="10.0.0.5")
    assert status == 404


def test_path_traversal_from_loopback_still_reaches_app():
    status = _raw_asgi_status(path="/api/../health", peer="127.0.0.1")
    assert status == 200


def test_ipv4_mapped_loopback_recognized():
    """Dual-stack binds deliver loopback as ::ffff:127.0.0.1 — tier 2
    must recognize it or every local consumer breaks."""
    status = _raw_asgi_status(path="/admin/status", peer="::ffff:127.0.0.1")
    assert status == 200


def test_websocket_denial_sends_close_frame():
    code = _raw_asgi_status(
        path="/api/v1/stream", peer="10.0.0.5", scope_type="websocket",
    )
    assert code == 4401  # 4000 + HTTP 401 (missing bearer token)


def test_websocket_with_token_reaches_inner_app():
    result = _raw_asgi_status(
        path="/api/v1/stream", peer="10.0.0.5", scope_type="websocket",
        headers=[(b"authorization", b"Bearer x.y.z")],
    )
    assert result == "accepted"


def test_cidr_allow_entry_matches_subnet():
    client = _app(
        verifier=_FakeVerifier(_principal()),
        allow=("10.0.0.0/24",),
        peer="10.0.0.77",
    )
    assert client.get("/api/v1/echo", headers=_AUTH).status_code == 200
