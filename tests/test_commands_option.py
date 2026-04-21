import json
from datetime import date, timedelta
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _future_spec() -> str:
    # Build a YYMMDD that's always ~1 year away.
    future = date.today() + timedelta(days=365)
    return future.strftime("%y%m%d")


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=1_000_000,
                         refresh_token_expires_at=2_000_000))


_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35, "change": 2.10, "percentChange": 1.50},
    "callExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00135000",
                "bid": 8.40, "ask": 8.50, "last": 8.45,
                "delta": 0.71, "gamma": 0.018, "theta": -0.04, "vega": 0.18,
                "volatility": 35.0, "strikePrice": 135.0, "inTheMoney": True,
                "totalVolume": 123, "openInterest": 456,
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
                "settlementType": "P",
            }],
        },
    },
    "putExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00135000",
                "bid": 0.42, "ask": 0.45, "last": 0.43,
                "delta": -0.12, "volatility": 38.0, "strikePrice": 135.0,
                "inTheMoney": False,
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
                "settlementType": "P",
            }],
        },
    },
}


def test_option_happy_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 0, result.output
    assert "NVDA" in result.output
    assert "STRIKE" in result.output


def test_option_invalid_spec_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", "abcdef"])
    assert result.exit_code == 2
    assert "Invalid option spec" in result.output


def test_option_past_expiry_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", "200115"])
    assert result.exit_code == 1
    assert "past" in result.output.lower()


def test_option_no_session_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_option_session_expired_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.option.get_chain",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_option_empty_chain_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    empty = {"symbol": "XYZZZ", "status": "FAILED",
             "callExpDateMap": {}, "putExpDateMap": {}}
    with patch("schwab_cli.commands.option.get_chain", return_value=empty):
        result = runner.invoke(app, ["option", "XYZZZ", _future_spec()])
    assert result.exit_code == 1
    assert "No options found" in result.output


def test_option_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["symbol"] == "NVDA"


def test_option_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--md"])
    assert result.exit_code == 0, result.output
    assert "# NVDA" in result.stdout


def test_option_json_md_mutex_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_option_detail_flag_routes_to_layout_b(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--detail=1"])
    assert result.exit_code == 0, result.output
    assert "Side" in result.output


def test_option_puts_only_auto_fallback(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    puts_only = dict(_CHAIN_RESP)
    puts_only = {**puts_only, "callExpDateMap": {}}
    with patch("schwab_cli.commands.option.get_chain", return_value=puts_only):
        result = runner.invoke(app, ["option", "NVDA", f"{_future_spec()}P*"])
    assert result.exit_code == 0, result.output


def test_option_exact_strike_spec_hits_chain_with_strike(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_get_chain(client, symbol, **kwargs):
        captured.update(kwargs)
        return _CHAIN_RESP

    with patch("schwab_cli.commands.option.get_chain", side_effect=fake_get_chain):
        result = runner.invoke(app, ["option", "NVDA", f"{_future_spec()}*135"])
    assert result.exit_code == 0, result.output
    assert captured["strike"] == 135.0
