"""Tests for the `greeks` command.

The command's job is to accept any ticker form, validate it's an option,
filter Schwab's chain response down to the one matching strike+side, and
render a one-contract detail view. We mock ``get_chain`` with a canned
response and verify:

  1. Every accepted ticker form reaches the API with the same parameters
     (symbol, strike, expiry, contract_type).
  2. HUMAN, JSON, and MD outputs differ and each contain the right shape.
  3. Stock tickers bounce with a clear error (exit 2).
  4. Missing strike in the chain response bounces (exit 1).
"""

import json
from unittest.mock import patch

from typer.testing import CliRunner

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
        access_token="atok",
        refresh_token="rtok",
        expires_at=1_000_000,
        refresh_token_expires_at=2_000_000,
    ))


# The chain response includes both the target (202.5) and a nearby strike
# (200.0) — the command must pick exactly the one matching the ticker.
_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 202.50, "change": 2.62, "percentChange": 1.31},
    "callExpDateMap": {
        "2026-05-01:9": {
            "202.5": [{
                "putCall": "CALL", "symbol": "NVDA  260501C00202500",
                "bid": 4.70, "ask": 4.80, "last": 4.75, "mark": 4.75,
                "delta": 0.510, "gamma": 0.035, "theta": -0.267, "vega": 0.125,
                "rho": 0.023, "volatility": 36.582, "strikePrice": 202.5,
                "totalVolume": 8809, "openInterest": 5174,
                "timeValue": 4.75, "intrinsicValue": 0.0, "inTheMoney": False,
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2026-05-01", "daysToExpiration": 9,
            }],
            "200.0": [{
                "putCall": "CALL", "symbol": "NVDA  260501C00200000",
                "bid": 6.15, "ask": 6.25, "last": 6.20, "mark": 6.20,
                "delta": 0.595, "gamma": 0.033, "theta": -0.260, "vega": 0.120,
                "volatility": 35.0, "strikePrice": 200.0,
                "totalVolume": 1, "openInterest": 1,
                "inTheMoney": True, "settlementType": "P",
                "expirationDate": "2026-05-01", "daysToExpiration": 9,
            }],
        },
    },
    "putExpDateMap": {},
}


def test_greeks_happy_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.greeks.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 0, result.output
    # Human rendering surfaces the canonical symbol, the strike, IV, and the
    # core greek labels (the Unicode glyphs are load-bearing for the UX).
    assert "NVDA  260501C00202500" in result.output
    assert "$202.50" in result.output
    assert "36.58%" in result.output
    for glyph in ("Δ", "Γ", "Θ", "𝒱", "ρ"):
        assert glyph in result.output


def test_greeks_json_output_is_parseable(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.greeks.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["underlyingSymbol"] == "NVDA"
    assert data["expiry"] == "2026-05-01"
    assert data["contract"]["optionSymbol"] == "NVDA  260501C00202500"
    assert data["contract"]["side"] == "C"
    assert data["contract"]["strike"] == 202.5
    assert data["contract"]["delta"] == 0.510
    # IV is returned as fraction (0.3658...), not percent.
    assert 0.35 < data["contract"]["iv"] < 0.37


def test_greeks_md_output_has_table_sections(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.greeks.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert result.exit_code == 0, result.output
    assert "## Quote" in result.output
    assert "## Greeks" in result.output
    assert "## Value" in result.output
    assert "| Bid |" in result.output
    assert "| Δ delta |" in result.output


def test_greeks_all_ticker_forms_same_api_call(monkeypatch, tmp_path):
    """Every accepted ticker form must send the same params to the API."""
    _prep(monkeypatch, tmp_path)
    captured: list[dict] = []

    def fake_get_chain(client, symbol, **kwargs):
        captured.append({"symbol": symbol, **kwargs})
        return _CHAIN_RESP

    forms = [
        "NVDA260501C202.5",
        "NVDA  260501C00202500",
        "NVDA260501C00202500",
    ]
    with patch("schwab_cli.commands.greeks.get_chain", side_effect=fake_get_chain):
        for form in forms:
            result = runner.invoke(app, ["greeks", form, "--json"])
            assert result.exit_code == 0, f"{form}: {result.output}"

    # All calls should have hit the API with identical shape.
    assert len(captured) == len(forms)
    first = captured[0]
    for c in captured[1:]:
        assert c == first, f"drift: {first} vs {c}"
    # And the shape should be the expected one for this contract.
    assert first["symbol"] == "NVDA"
    assert first["contract_type"] == "CALL"
    assert first["strike"] == 202.5
    assert first["from_date"].isoformat() == "2026-05-01"
    assert first["to_date"].isoformat() == "2026-05-01"


def test_greeks_rejects_stock_ticker(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "NVDA"])
    assert result.exit_code == 2
    assert "not an option ticker" in result.output


def test_greeks_rejects_unparseable_ticker(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "NVDA-260501C240"])
    assert result.exit_code == 2
    assert "unrecognized ticker" in result.output.lower()


def test_greeks_missing_strike_returns_exit_1(monkeypatch, tmp_path):
    """If the chain response carries no contract at the requested strike +
    side, the command exits 1 with a clear message."""
    _prep(monkeypatch, tmp_path)
    # Response with only the 200.0 call — no 202.5 strike.
    resp = {
        "underlying": _CHAIN_RESP["underlying"],
        "callExpDateMap": {
            "2026-05-01:9": {
                "200.0": _CHAIN_RESP["callExpDateMap"]["2026-05-01:9"]["200.0"],
            },
        },
        "putExpDateMap": {},
    }
    with patch("schwab_cli.commands.greeks.get_chain", return_value=resp):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1
    assert "No CALL contract" in result.output
    assert "202.50" in result.output


def test_greeks_put_side_selects_put_variant(monkeypatch, tmp_path):
    """A put ticker must skip the call-side entries and pick the put row."""
    _prep(monkeypatch, tmp_path)
    put_row = {
        "putCall": "PUT", "symbol": "NVDA  260501P00202500",
        "bid": 4.55, "ask": 4.65, "last": 4.60, "mark": 4.60,
        "delta": -0.489, "gamma": 0.035, "theta": -0.270, "vega": 0.125,
        "volatility": 36.582, "strikePrice": 202.5,
        "totalVolume": 2199, "openInterest": 2940,
        "timeValue": 4.60, "intrinsicValue": 0.0, "inTheMoney": False,
        "multiplier": 100, "settlementType": "P",
        "expirationDate": "2026-05-01", "daysToExpiration": 9,
    }
    resp = {
        "underlying": _CHAIN_RESP["underlying"],
        "callExpDateMap": {},
        "putExpDateMap": {
            "2026-05-01:9": {"202.5": [put_row]},
        },
    }
    with patch("schwab_cli.commands.greeks.get_chain", return_value=resp):
        result = runner.invoke(app, ["greeks", "NVDA260501P202.5", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["contract"]["side"] == "P"
    assert data["contract"]["delta"] == -0.489
