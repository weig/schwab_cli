"""Tests for the public /mcp surface (remote connector, plan Phase 1).

`web.resource_url` is the master switch: unset → behavior identical to
today (/mcp loopback-only, PRM 404). Set → non-loopback /mcp requires
peer-allowlist + Bearer JWT (existing verifier), 401s carry the RFC 9728
``resource_metadata`` pointer, and the Protected Resource Metadata document
is served to allowed peers.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from schwab_cli.webauth.middleware import WebAuthMiddleware
from schwab_cli.webauth.verify import InvalidToken, Principal, SubjectNotAllowed

RESOURCE = "https://schwab.example.com"
PRM_PATH = "/.well-known/oauth-protected-resource"
_AUTH = {"Authorization": "Bearer x.y.z"}


def _principal() -> Principal:
    return Principal(provider="auth0", subject="auth0|me", email=None,
                     scopes=frozenset(("marketdata",)))


class _FakeVerifier:
    def __init__(self, outcome) -> None:
        self._outcome = outcome

    def verify(self, token: str):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def _mcp(request):
    p = request.scope.get("state", {}).get("principal")
    return JSONResponse({"tool": "ok", "sub": p.subject if p else None})


def _app(*, verifier=None, peer="127.0.0.1", resource_url=RESOURCE,
         has_providers=True,
         allow=("127.0.0.1", "::1", "192.168.2.1")) -> TestClient:
    inner = Starlette(routes=[
        Route("/mcp", _mcp, methods=["GET", "POST"]),
        Route("/admin/status", _mcp),
    ])
    wrapped = WebAuthMiddleware(
        inner,
        verifier=verifier if verifier is not None else _FakeVerifier(_principal()),
        has_providers=has_providers,
        allow=allow,
        peer_of=lambda scope: peer,
        mcp_resource_url=resource_url,
        issuers=("https://zingrun.us.auth0.com/",),
    )
    return TestClient(wrapped, raise_server_exceptions=True)


# ---- master switch off → today's behavior, pinned -----------------------

def test_switch_off_mcp_remote_404():
    c = _app(peer="192.168.2.1", resource_url=None)
    assert c.post("/mcp", headers=_AUTH).status_code == 404


def test_switch_off_prm_404():
    c = _app(peer="192.168.2.1", resource_url=None)
    assert c.get(PRM_PATH).status_code == 404


def test_loopback_mcp_unaffected_regardless_of_switch():
    for ru in (None, RESOURCE):
        c = _app(peer="127.0.0.1", resource_url=ru)
        r = c.post("/mcp")  # no token needed on loopback
        assert r.status_code == 200
        assert r.json()["sub"] is None


# ---- switch on: /mcp non-loopback gate ----------------------------------

def test_remote_mcp_peer_not_allowed_404():
    c = _app(peer="203.0.113.9")  # not in allowlist
    assert c.post("/mcp", headers=_AUTH).status_code == 404


def test_remote_mcp_no_token_401_with_resource_metadata():
    c = _app(peer="192.168.2.1")
    r = c.post("/mcp")
    assert r.status_code == 401
    www = r.headers.get("WWW-Authenticate", "")
    assert f'resource_metadata="{RESOURCE}{PRM_PATH}"' in www
    assert www.startswith("Bearer")


def test_remote_mcp_invalid_token_401_with_resource_metadata():
    c = _app(peer="192.168.2.1", verifier=_FakeVerifier(InvalidToken("bad")))
    r = c.post("/mcp", headers=_AUTH)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("WWW-Authenticate", "")


def test_remote_mcp_subject_not_allowed_403():
    c = _app(peer="192.168.2.1", verifier=_FakeVerifier(SubjectNotAllowed("no")))
    assert c.post("/mcp", headers=_AUTH).status_code == 403


def test_remote_mcp_valid_token_passes_with_principal():
    c = _app(peer="192.168.2.1")
    r = c.post("/mcp", headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["sub"] == "auth0|me"


def test_remote_mcp_no_providers_503():
    c = _app(peer="192.168.2.1", verifier=None, has_providers=False)
    assert c.post("/mcp", headers=_AUTH).status_code == 503


# ---- PRM document --------------------------------------------------------

def test_prm_served_to_allowed_peer_and_loopback():
    for peer in ("192.168.2.1", "127.0.0.1"):
        c = _app(peer=peer)
        r = c.get(PRM_PATH)
        assert r.status_code == 200
        body = r.json()
        assert body["resource"] == RESOURCE
        assert body["authorization_servers"] == ["https://zingrun.us.auth0.com/"]


def test_prm_needs_no_token():
    c = _app(peer="192.168.2.1")
    assert c.get(PRM_PATH).status_code == 200  # no Authorization header


def test_prm_hidden_from_unallowed_peer():
    c = _app(peer="203.0.113.9")
    assert c.get(PRM_PATH).status_code == 404


# ---- control plane stays closed ------------------------------------------

def test_admin_still_404_remote_even_with_switch_on():
    c = _app(peer="192.168.2.1")
    assert c.get("/admin/status", headers=_AUTH).status_code == 404


# ---- config round-trip ----------------------------------------------------

def test_config_resource_url_roundtrip(tmp_path, monkeypatch):
    from schwab_cli.config import Config, load, save

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg = Config(client_id="a", client_secret="b",
                 redirect_uri="https://127.0.0.1:8443",
                 web_allow=("127.0.0.1", "::1", "192.168.2.1"),
                 web_resource_url="https://schwab.example.com/")
    save(cfg)
    back = load()
    assert back.web_resource_url == "https://schwab.example.com"  # trailing / stripped
    assert "192.168.2.1" in back.web_allow


def test_config_resource_url_must_be_https(tmp_path, monkeypatch):
    import json
    import pytest as _pytest

    from schwab_cli.config import ConfigError, config_path, load

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1, "client_id": "a", "client_secret": "b",
        "redirect_uri": "https://127.0.0.1:8443",
        "web": {"allow": ["127.0.0.1"], "resource_url": "http://insecure"},
    }))
    with _pytest.raises(ConfigError):
        load()
