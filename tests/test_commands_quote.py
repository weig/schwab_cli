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
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    ))


_QUOTES = {
    "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
}


def test_quote_happy_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.quote.get_quotes", return_value=_QUOTES):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "232.14" in result.output


def test_quote_multi_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    payload = {
        "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
        "MSFT": {"symbol": "MSFT", "quote": {"lastPrice": 451.22}},
    }
    with patch("schwab_cli.commands.quote.get_quotes", return_value=payload):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT"])
    assert result.exit_code == 0
    assert "AAPL" in result.output
    assert "MSFT" in result.output


def test_quote_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.quote.get_quotes", return_value=_QUOTES):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"


def test_quote_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.quote.get_quotes", return_value=_QUOTES):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    assert result.exit_code == 0
    assert "| Symbol" in result.stdout


def test_quote_both_flags_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote", "AAPL", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_quote_no_symbols_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote"])
    assert result.exit_code != 0  # typer missing-argument exit


def test_quote_no_session_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_quote_session_expired_message(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.quote.get_quotes",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_quote_api_error_surfaces(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.quote.get_quotes",
        side_effect=ApiError("500 internal"),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1
    assert "500" in result.output
