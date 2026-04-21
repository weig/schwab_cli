from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    ))


_ACCOUNTS = [
    {"securitiesAccount": {
        "accountNumber": "12345678", "type": "MARGIN",
        "currentBalances": {"liquidationValue": 1000.0, "cashBalance": 500.0},
        "positions": [],
    }},
]


def test_accounts_no_session_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "No session found" in result.output


def test_accounts_no_config_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "No config" in result.output or "No session" in result.output


def test_accounts_happy_path_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0, result.output
    assert "12345678" in result.output or "5678" in result.output
    assert "MARGIN" in result.output


def test_accounts_json_flag_outputs_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data[0]["accountNumber"] == "12345678"


def test_accounts_md_flag_outputs_md(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts", "--md"])
    assert result.exit_code == 0
    assert "| Account" in result.stdout


def test_accounts_both_flags_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["accounts", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_accounts_session_expired_message(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_accounts_api_error_surfaces(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=ApiError("500 internal server error"),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "500" in result.output
