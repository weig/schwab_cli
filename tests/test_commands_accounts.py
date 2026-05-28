import time
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
        expires_at=int(time.time()) + 3600,
        refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
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
    with patch("schwab_cli.api.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0, result.output
    assert "12345678" in result.output or "5678" in result.output
    assert "MARGIN" in result.output


def test_accounts_json_flag_outputs_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data[0]["accountNumber"] == "12345678"


def test_accounts_md_flag_outputs_md(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.list_accounts", return_value=_ACCOUNTS):
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
        "schwab_cli.api.accounts.list_accounts",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_accounts_api_error_surfaces(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.accounts.list_accounts",
        side_effect=ApiError("500 internal server error"),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "500" in result.output


_SINGLE_ACCOUNT = {"securitiesAccount": {
    "accountNumber": "12345678", "type": "MARGIN",
    "currentBalances": {"liquidationValue": 2000.0, "cashBalance": 800.0, "buyingPower": 4000.0},
    "initialBalances": {"cashBalance": 800.0},
    "positions": [],
}}


def test_account_show_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.get_account", return_value=_SINGLE_ACCOUNT):
        result = runner.invoke(app, ["account", "12345678"])
    assert result.exit_code == 0, result.output
    assert "MARGIN" in result.output


def test_account_show_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.get_account", return_value=_SINGLE_ACCOUNT):
        result = runner.invoke(app, ["account", "5678", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data["accountNumber"] == "12345678"


def test_account_show_api_error(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.accounts.get_account",
        side_effect=ApiError("Account '99' not found. Available: ...5678."),
    ):
        result = runner.invoke(app, ["account", "99"])
    assert result.exit_code == 1
    assert "not found" in result.output


_POSITION_ROWS = [
    {
        "_account": "12345678",
        "instrument": {"symbol": "AAPL"},
        "longQuantity": 10.0,
        "averagePrice": 200.0,
        "marketValue": 2321.40,
    },
]


def test_positions_all_accounts_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.get_positions", return_value=_POSITION_ROWS):
        result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output


def test_positions_filtered_account(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured_arg: list[str | None] = []

    def fake_get_positions(client, account):
        captured_arg.append(account)
        return _POSITION_ROWS

    with patch("schwab_cli.api.accounts.get_positions", side_effect=fake_get_positions):
        result = runner.invoke(app, ["positions", "5678"])
    assert result.exit_code == 0
    assert captured_arg == ["5678"]


def test_positions_md_flag(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.accounts.get_positions", return_value=_POSITION_ROWS):
        result = runner.invoke(app, ["positions", "--md"])
    assert result.exit_code == 0
    assert "| Account" in result.stdout
