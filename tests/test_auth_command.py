from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.oauth import OAuthError, TokenResponse
from schwab_cli.session import Session, load as load_session, save as save_session

runner = CliRunner()


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _seed_config(username="user@example.com", password="op://X/Y/Z"):
    save_config(Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        username=username,
        password=password,
    ))


def _seed_session():
    save_session(Session(
        access_token="old_a", refresh_token="old_r",
        expires_at=100, refresh_token_expires_at=200,
    ))


def test_auth_errors_when_no_config(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "Run `schwab_cli setup` first" in result.output


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
         patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
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
         patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr):
        result = runner.invoke(app, ["auth", "--force"])

    assert result.exit_code == 0, result.output
    refresh_mock.assert_not_called()
    s = load_session()
    assert s.access_token == "full_a"


def test_auth_full_auth_failure_exits_1_no_session_written(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    # No prior session.

    from schwab_cli.browser.flow import AuthError

    with patch("schwab_cli.commands.auth.run_full_auth",
               side_effect=AuthError("Login failed — incorrect username/password.")):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "Login failed" in result.output
    assert load_session() is None


def test_auth_full_auth_runs_when_no_session(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    with patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE") as full, \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr) as ex:
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    full.assert_called_once()
    ex.assert_called_once()
    assert "Authenticated" in result.output


def test_auth_token_exchange_failure_after_full_auth(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(400, request=req, json={"error": "invalid_grant"})
    err = httpx.HTTPStatusError("400", request=req, response=resp)

    with patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", side_effect=err):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "Token exchange failed" in result.output
    assert load_session() is None
