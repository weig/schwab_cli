"""Tests for ``schwab_cli auth`` command orchestration.

We mock out :func:`schwab_cli.commands.auth.get_auth_response` (the
function that opens the browser and races handlers) and verify the
glue: config/session loading, refresh-or-fresh decision, exchange of
``code`` results, direct wrap of ``token`` results, error surfacing.
"""
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from schwab_cli.auth_flows import AuthFlowError
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.oauth import TokenResponse
from schwab_cli.session import Session, load as load_session, save as save_session

runner = CliRunner()


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)


def _seed_config():
    save_config(Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com/uuid/callback",
        auth_flow="code_relay",
        code_relay_url="https://relay.example.com/uuid/wait",
    ))


def _seed_session():
    save_session(Session(
        access_token="old_a", refresh_token="old_r",
        expires_at=100, refresh_token_expires_at=200,
    ))


_CODE_RESULT = {"kind": "code", "code": "CODE", "state": "S"}


def test_auth_errors_when_no_config(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "Run `schwab_cli setup` first" in result.output


def test_auth_errors_when_config_malformed(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid json")

    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "Config is unusable" in result.output
    assert "setup" in result.output


def test_auth_falls_back_to_full_auth_on_malformed_session(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    (cfg_dir / "session.json").write_text("{not valid")

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)
    with patch("schwab_cli.commands.auth.get_auth_response", return_value=_CODE_RESULT), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Stored session is unreadable" in result.output
    assert "Authenticated" in result.output
    s = load_session()
    assert s.access_token == "full_a"


def test_auth_refreshes_when_session_present(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="new_a", refresh_token="new_r", expires_in=1800)
    with patch("schwab_cli.commands.auth.oauth.refresh", return_value=fake_tr):
        with patch("schwab_cli.commands.auth.time.time", return_value=1_000_000):
            result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Already logged in" in result.output
    s = load_session()
    assert s.access_token == "new_a"
    assert s.refresh_token == "new_r"
    assert s.expires_at == 1_000_000 + 1800


def test_auth_falls_back_to_full_auth_on_refresh_failure(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(401, request=req, json={"error": "invalid_grant"})
    refresh_err = httpx.HTTPStatusError("401", request=req, response=resp)

    with patch("schwab_cli.commands.auth.oauth.refresh", side_effect=refresh_err), \
         patch("schwab_cli.commands.auth.get_auth_response", return_value=_CODE_RESULT), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr), \
         patch("schwab_cli.commands.auth.time.time", return_value=2_000_000):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Refresh token rejected" in result.output
    assert "Authenticated" in result.output
    s = load_session()
    assert s.access_token == "full_a"
    assert s.expires_at == 2_000_000 + 1800


def test_auth_force_skips_refresh(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    with patch("schwab_cli.commands.auth.oauth.refresh") as refresh_mock, \
         patch("schwab_cli.commands.auth.get_auth_response", return_value=_CODE_RESULT), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr):
        result = runner.invoke(app, ["auth", "--force"])

    assert result.exit_code == 0, result.output
    refresh_mock.assert_not_called()
    s = load_session()
    assert s.access_token == "full_a"


def test_auth_full_auth_failure_exits_1_no_session_written(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    with patch(
        "schwab_cli.commands.auth.get_auth_response",
        side_effect=AuthFlowError("all auth handlers failed"),
    ):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "all auth handlers failed" in result.output
    assert load_session() is None


def test_auth_full_auth_runs_when_no_session(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    with patch(
        "schwab_cli.commands.auth.get_auth_response", return_value=_CODE_RESULT,
    ) as full, patch(
        "schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr,
    ) as ex:
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    full.assert_called_once()
    ex.assert_called_once()
    assert "Authenticated" in result.output


def test_auth_token_kind_skips_exchange(monkeypatch, tmp_path):
    """When a handler returns ``kind="token"`` (future AuthServerHandler),
    the command must NOT call ``oauth.exchange_code`` — it should wrap the
    bundle directly and save the session."""
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    token_result = {
        "kind": "token",
        "access_token": "direct_a",
        "refresh_token": "direct_r",
        "expires_in": 1800,
    }

    with patch(
        "schwab_cli.commands.auth.get_auth_response", return_value=token_result,
    ), patch(
        "schwab_cli.commands.auth.oauth.exchange_code",
    ) as ex:
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    ex.assert_not_called()
    s = load_session()
    assert s.access_token == "direct_a"
    assert s.refresh_token == "direct_r"


def test_auth_token_exchange_failure_after_full_auth(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(400, request=req, json={"error": "invalid_grant"})
    err = httpx.HTTPStatusError("400", request=req, response=resp)

    with patch(
        "schwab_cli.commands.auth.get_auth_response", return_value=_CODE_RESULT,
    ), patch(
        "schwab_cli.commands.auth.oauth.exchange_code", side_effect=err,
    ):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "Token exchange failed" in result.output
    assert load_session() is None


def test_manual_flag_propagates_to_get_auth_response(monkeypatch, tmp_path):
    """``--manual`` is meaningful again — it's passed through to
    ``get_auth_response(cfg, manual=True)`` which then skips the auto-login
    subprocess. Asserted by capturing the kwarg."""
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    fake_tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1800)
    captured = {}

    def fake_get_auth_response(cfg, *, manual=False):
        captured["manual"] = manual
        return _CODE_RESULT

    with patch(
        "schwab_cli.commands.auth.get_auth_response",
        side_effect=fake_get_auth_response,
    ), patch(
        "schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr,
    ):
        result = runner.invoke(app, ["auth", "--manual"])
    assert result.exit_code == 0, result.output
    assert captured["manual"] is True


def test_no_manual_flag_passes_manual_false(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    fake_tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1800)
    captured = {}

    def fake_get_auth_response(cfg, *, manual=False):
        captured["manual"] = manual
        return _CODE_RESULT

    with patch(
        "schwab_cli.commands.auth.get_auth_response",
        side_effect=fake_get_auth_response,
    ), patch(
        "schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr,
    ):
        result = runner.invoke(app, ["auth"])
    assert result.exit_code == 0, result.output
    assert captured["manual"] is False


def test_auth_surfaces_oauth_authorization_error(monkeypatch, tmp_path):
    """When a handler returns ``kind="error"``, ``resolve_auth_result``
    raises ``OAuthAuthorizationError`` and ``auth.run`` prints a clear
    message and exits 1 — without writing a session."""
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    error_result = {
        "kind": "error",
        "error": "access_denied",
        "error_description": "user rejected consent",
        "state": "S",
    }
    with patch(
        "schwab_cli.commands.auth.get_auth_response", return_value=error_result,
    ):
        result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "OAuth error from Schwab" in result.output
    assert "access_denied" in result.output
    assert "user rejected consent" in result.output
    assert load_session() is None
