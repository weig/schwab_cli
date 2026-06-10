"""P2 wiring tests: runtime gate, /api/v1 routes, config web.allow,
doctor rows, and the `schwab webauth` CLI commands."""
from __future__ import annotations

import json
import time

import jwt as pyjwt
import pytest
import respx
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from typer.testing import CliRunner

from schwab_cli.commands.doctor import _webauth_provider_rows
from schwab_cli.config import Config, ConfigError
from schwab_cli.webauth.config import LoadedProviders, ProviderError, load_providers
from schwab_cli.webauth.middleware import WebAuthMiddleware
from schwab_cli.webauth.runtime import build_gate
from schwab_cli.webauth.verify import Principal

runner = CliRunner()

_ISS = "https://tenant.us.auth0.com/"
_AUD = "https://schwab-api.local"


# ---------------------------------------------------------------------------
# runtime.build_gate
# ---------------------------------------------------------------------------


def test_build_gate_warns_per_bad_file(monkeypatch, tmp_path):
    d = tmp_path / "webauth"
    d.mkdir()
    (d / "bad.json").write_text("{nope")
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    warnings: list[str] = []
    wrap, loaded = build_gate(allow=("127.0.0.1",), warn=warnings.append)
    assert loaded.providers == ()
    assert len(warnings) == 1 and "bad.json" in warnings[0]
    # wrapper still functions in legacy mode
    assert callable(wrap)


def test_build_gate_no_dir_legacy_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    wrap, loaded = build_gate(allow=("127.0.0.1",), warn=None)
    assert loaded.providers == ()


# ---------------------------------------------------------------------------
# /api/v1 routes through the middleware
# ---------------------------------------------------------------------------


class _GrantVerifier:
    def __init__(self, scopes) -> None:
        self._scopes = frozenset(scopes)

    def verify(self, token: str) -> Principal:
        return Principal(
            provider="auth0", subject="auth0|abc", email=None,
            scopes=self._scopes,
        )


def _api_client(monkeypatch, *, scopes) -> TestClient:
    from schwab_cli.server.rest import build_rest_app

    monkeypatch.setattr(
        "schwab_cli.service.quotes.QuoteService.get_quote_payload",
        lambda self, symbols: {"SPY": {"symbol": "SPY", "last": 600.0}},
    )
    app = WebAuthMiddleware(
        build_rest_app(),
        verifier=_GrantVerifier(scopes),
        has_providers=True,
        allow=("127.0.0.1",),
        peer_of=lambda scope: "127.0.0.1",
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer x.y.z"}


def test_api_quote_with_marketdata_scope(monkeypatch):
    client = _api_client(monkeypatch, scopes=("marketdata",))
    resp = client.get("/api/v1/quote/spy", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["SPY"]["last"] == 600.0


def test_api_quote_without_marketdata_scope_is_403(monkeypatch):
    client = _api_client(monkeypatch, scopes=("accounts",))
    resp = client.get("/api/v1/quote/spy", headers=_AUTH)
    assert resp.status_code == 403
    assert "marketdata" in resp.json()["error"]


def test_api_health_passes_without_token(monkeypatch):
    client = _api_client(monkeypatch, scopes=())
    assert client.get("/api/v1/health").status_code == 200


def test_legacy_quote_route_still_loopback(monkeypatch):
    client = _api_client(monkeypatch, scopes=())
    # tier 2: loopback peer reaches the legacy PoC route unauthenticated
    assert client.get("/quote/spy").status_code == 200


# ---------------------------------------------------------------------------
# Config web.allow
# ---------------------------------------------------------------------------


def _write_config(tmp_path, extra: dict) -> None:
    payload = {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "local_server",
    }
    payload.update(extra)
    (tmp_path / "config.json").write_text(json.dumps(payload))


def test_config_web_allow_default(monkeypatch, tmp_path):
    from schwab_cli import config as config_module

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    _write_config(tmp_path, {})
    cfg = config_module.load()
    assert cfg.web_allow == ("127.0.0.1", "::1")


def test_config_web_allow_parsed(monkeypatch, tmp_path):
    from schwab_cli import config as config_module

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    _write_config(tmp_path, {"web": {"allow": ["127.0.0.1", "100.64.0.7"]}})
    cfg = config_module.load()
    assert cfg.web_allow == ("127.0.0.1", "100.64.0.7")


def test_config_web_allow_invalid_raises(monkeypatch, tmp_path):
    from schwab_cli import config as config_module

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    _write_config(tmp_path, {"web": {"allow": "127.0.0.1"}})
    with pytest.raises(ConfigError, match="web.allow"):
        config_module.load()


def test_config_web_allow_accepts_cidr(monkeypatch, tmp_path):
    from schwab_cli import config as config_module

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    _write_config(tmp_path, {"web": {"allow": ["100.64.0.0/10"]}})
    assert config_module.load().web_allow == ("100.64.0.0/10",)


def test_config_web_allow_rejects_hostname(monkeypatch, tmp_path):
    """A typo'd hostname would never match any peer and silently lock
    the proxy out — fail loudly at load time instead."""
    from schwab_cli import config as config_module

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    _write_config(tmp_path, {"web": {"allow": ["nginx-proxy"]}})
    with pytest.raises(ConfigError, match="nginx-proxy"):
        config_module.load()


def test_config_web_roundtrips_through_payload():
    cfg = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        web_allow=("127.0.0.1", "10.0.0.5"),
    )
    assert cfg.to_payload()["web"] == {"allow": ["127.0.0.1", "10.0.0.5"]}


# ---------------------------------------------------------------------------
# doctor rows
# ---------------------------------------------------------------------------


def test_doctor_rows_empty_is_single_info():
    rows = _webauth_provider_rows(LoadedProviders(providers=(), errors=()))
    assert rows[0][0] == "info"


def test_doctor_rows_render_ok_bad_and_disabled(monkeypatch, tmp_path):
    d = tmp_path / "webauth"
    d.mkdir()
    (d / "good.json").write_text(json.dumps({
        "issuer": _ISS, "audience": _AUD, "allowed_subjects": ["a"],
    }))
    (d / "bad.json").write_text("{nope")
    (d / "off.json").write_text(json.dumps({
        "issuer": "https://x.example/", "audience": "a", "enabled": False,
    }))
    rows = _webauth_provider_rows(load_providers(d))
    statuses = {label: status for status, label, _ in rows}
    assert statuses["good"] == "ok"
    assert statuses["bad.json"] == "bad"
    assert statuses["off"] == "info"


# ---------------------------------------------------------------------------
# CLI: schwab webauth list / verify
# ---------------------------------------------------------------------------


def _seed_provider(tmp_path, monkeypatch, **over) -> None:
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    d = tmp_path / "webauth"
    d.mkdir(exist_ok=True)
    payload = {
        "issuer": _ISS,
        "audience": _AUD,
        "jwks_uri": "https://keys.example/jwks",
        "allowed_subjects": {"auth0|abc": ["dataset"]},
    }
    payload.update(over)
    (d / "auth0.json").write_text(json.dumps(payload))


def test_cli_webauth_list(monkeypatch, tmp_path):
    from schwab_cli.cli import app

    _seed_provider(tmp_path, monkeypatch)
    result = runner.invoke(app, ["webauth", "list"])
    assert result.exit_code == 0, result.output
    assert "auth0" in result.output
    assert _ISS in result.output


def test_cli_webauth_list_empty(monkeypatch, tmp_path):
    from schwab_cli.cli import app

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["webauth", "list"])
    assert result.exit_code == 0
    assert "No providers" in result.output


@respx.mock
def test_cli_webauth_verify_roundtrip(monkeypatch, tmp_path):
    """End-to-end: a self-signed token verifies through the same path
    the REST middleware uses (JWKS served via respx)."""
    from jwt.algorithms import RSAAlgorithm

    from schwab_cli.cli import app

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    respx.get("https://keys.example/jwks").mock(
        return_value=httpx.Response(200, json={"keys": [jwk]}),
    )

    _seed_provider(tmp_path, monkeypatch)
    now = int(time.time())
    token = pyjwt.encode(
        {
            "iss": _ISS, "aud": _AUD, "sub": "auth0|abc",
            "exp": now + 600, "iat": now, "scope": "marketdata",
        },
        pem, algorithm="RS256", headers={"kid": "k1"},
    )
    result = runner.invoke(app, ["webauth", "verify", token])
    assert result.exit_code == 0, result.output
    assert "ACCEPTED" in result.output
    assert "dataset" in result.output      # static grant
    assert "marketdata" in result.output   # token scope


def test_cli_webauth_verify_rejects_garbage(monkeypatch, tmp_path):
    from schwab_cli.cli import app

    _seed_provider(tmp_path, monkeypatch)
    result = runner.invoke(app, ["webauth", "verify", "not.a.jwt"])
    assert result.exit_code == 1
    assert "REJECTED" in result.output


def test_cli_webauth_verify_no_providers(monkeypatch, tmp_path):
    from schwab_cli.cli import app

    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["webauth", "verify", "x.y.z"])
    assert result.exit_code == 1
    assert "No usable providers" in result.output
