import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=int(time.time()) + 3600,
                         refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600))


_RAW = {
    "symbol": "NVDA",
    "empty": False,
    "previousClose": 100.0,
    "candles": [
        {
            "datetime": _ms(datetime(2024, 4, 22, 13, 30, tzinfo=timezone.utc)),
            "open": 100.50, "high": 101.90, "low": 100.10,
            "close": 101.00, "volume": 1_000_000,
        },
        {
            "datetime": _ms(datetime(2024, 4, 23, 13, 30, tzinfo=timezone.utc)),
            "open": 101.00, "high": 101.50, "low":  99.80,
            "close": 100.00, "volume": 1_200_000,
        },
    ],
}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_history_default_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.history.get_history", return_value=_RAW):
        result = runner.invoke(app, ["history", "NVDA"])
    assert result.exit_code == 0, result.output
    assert "NVDA" in result.output
    assert "Date" in result.output


def test_history_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.history.get_history", return_value=_RAW):
        result = runner.invoke(app, ["history", "NVDA", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["symbol"] == "NVDA"
    assert data["interval"] == "1day"


def test_history_md(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.history.get_history", return_value=_RAW):
        result = runner.invoke(app, ["history", "NVDA", "--md"])
    assert result.exit_code == 0, result.output
    assert "# NVDA" in result.stdout


def test_history_custom_range_and_interval(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_get_history(client, symbol, **kwargs):
        captured.update(kwargs)
        return _RAW

    with patch("schwab_cli.api.history.get_history", side_effect=fake_get_history):
        result = runner.invoke(app, [
            "history", "NVDA",
            "--range=20240101..20240630", "--interval=1wk",
        ])
    assert result.exit_code == 0, result.output
    assert captured["frequency_type"] == "weekly"
    assert captured["frequency"] == 1


def test_history_symbol_uppercased(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_get_history(client, symbol, **kwargs):
        captured["symbol"] = symbol
        return _RAW

    with patch("schwab_cli.api.history.get_history", side_effect=fake_get_history):
        result = runner.invoke(app, ["history", "nvda"])
    assert result.exit_code == 0, result.output
    assert captured["symbol"] == "NVDA"


# ---------------------------------------------------------------------------
# Error matrix
# ---------------------------------------------------------------------------

def test_history_json_md_mutex_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["history", "NVDA", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_history_invalid_interval_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["history", "NVDA", "--interval=2min"])
    assert result.exit_code == 2
    assert "1min" in result.output


def test_history_invalid_range_grammar_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["history", "NVDA", "--range=garbage"])
    assert result.exit_code == 2


def test_history_inverted_range_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, [
        "history", "NVDA", "--range=20240601..20240101",
    ])
    assert result.exit_code == 1
    assert "start must be before end" in result.output


def test_history_future_start_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, [
        "history", "NVDA", "--range=20990101..20990102",
    ])
    assert result.exit_code == 1
    assert "future" in result.output.lower()


def test_history_no_config_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # No config/session saved.
    result = runner.invoke(app, ["history", "NVDA"])
    assert result.exit_code == 1
    assert "No config" in result.output


def test_history_no_session_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["history", "NVDA"])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_history_session_expired_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.history.get_history",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["history", "NVDA"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_history_api_error_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.history.get_history",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["history", "NVDA"])
    assert result.exit_code == 1
    assert "503" in result.output


def test_history_empty_candles_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    empty = {"symbol": "XYZZZ", "empty": True, "candles": []}
    with patch("schwab_cli.api.history.get_history", return_value=empty):
        result = runner.invoke(app, ["history", "XYZZZ"])
    assert result.exit_code == 1
    assert "No candles" in result.output
