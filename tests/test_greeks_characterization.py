"""Characterization tests for the `schwab greeks` command.

These tests pin the CURRENT observable behaviour of the greeks command
end-to-end so that the upcoming service-layer migration can be proven
behaviour-preserving without altering production code.

Seam used: ``schwab_cli.api.client.SchwabClient.get`` — patching at this
level exercises the full stack (ticker parsing, get_chain call, shape_envelope,
_pick_contract, render_greeks) while avoiding real HTTP traffic.  This seam
survives the refactor because the new service layer still routes through
``SchwabClient.get``.

Golden values were captured by running the current code and recording its
output verbatim.  Do NOT alter golden constants without first verifying that
the production code changed intentionally.
"""

from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prep(monkeypatch, tmp_path):
    """Isolated HOME with a valid config + non-expired session.

    The session's ``expires_at`` is set to now+3600 so the service-layer
    auth path (``service.auth.get_session``) does NOT attempt a real
    ``oauth.refresh`` — it only mints when the access token looks expired.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


# ---------------------------------------------------------------------------
# Canned /chains payloads
# ---------------------------------------------------------------------------

# Primary payload: two strikes (202.5 and 200.0) in callExpDateMap; no puts.
# The command is invoked with NVDA260501C202.5, so _pick_contract must select
# exactly the 202.5 strike CALL and ignore the 200.0 entry.
_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {
        "symbol": "NVDA",
        "last": 202.50,
        "change": 2.62,
        "percentChange": 1.31,
    },
    "callExpDateMap": {
        "2026-05-01:9": {
            "202.5": [
                {
                    "putCall": "CALL",
                    "symbol": "NVDA  260501C00202500",
                    "bid": 4.70,
                    "ask": 4.80,
                    "last": 4.75,
                    "mark": 4.75,
                    "delta": 0.510,
                    "gamma": 0.035,
                    "theta": -0.267,
                    "vega": 0.125,
                    "rho": 0.023,
                    "volatility": 36.582,
                    "strikePrice": 202.5,
                    "totalVolume": 8809,
                    "openInterest": 5174,
                    "timeValue": 4.75,
                    "intrinsicValue": 0.0,
                    "inTheMoney": False,
                    "multiplier": 100,
                    "settlementType": "P",
                    "expirationDate": "2026-05-01",
                    "daysToExpiration": 9,
                }
            ],
            "200.0": [
                {
                    "putCall": "CALL",
                    "symbol": "NVDA  260501C00200000",
                    "bid": 6.15,
                    "ask": 6.25,
                    "last": 6.20,
                    "mark": 6.20,
                    "delta": 0.595,
                    "gamma": 0.033,
                    "theta": -0.260,
                    "vega": 0.120,
                    "volatility": 35.0,
                    "strikePrice": 200.0,
                    "totalVolume": 1,
                    "openInterest": 1,
                    "inTheMoney": True,
                    "settlementType": "P",
                    "expirationDate": "2026-05-01",
                    "daysToExpiration": 9,
                }
            ],
        },
    },
    "putExpDateMap": {},
}

# PUT-side payload — only the 202.5 PUT is present.
_PUT_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {
        "symbol": "NVDA",
        "last": 202.50,
        "change": 2.62,
        "percentChange": 1.31,
    },
    "callExpDateMap": {},
    "putExpDateMap": {
        "2026-05-01:9": {
            "202.5": [
                {
                    "putCall": "PUT",
                    "symbol": "NVDA  260501P00202500",
                    "bid": 4.55,
                    "ask": 4.65,
                    "last": 4.60,
                    "mark": 4.60,
                    "delta": -0.489,
                    "gamma": 0.035,
                    "theta": -0.270,
                    "vega": 0.125,
                    "volatility": 36.582,
                    "strikePrice": 202.5,
                    "totalVolume": 2199,
                    "openInterest": 2940,
                    "timeValue": 4.60,
                    "intrinsicValue": 0.0,
                    "inTheMoney": False,
                    "multiplier": 100,
                    "settlementType": "P",
                    "expirationDate": "2026-05-01",
                    "daysToExpiration": 9,
                }
            ],
        },
    },
}

# Payload with only the 200.0 call — no 202.5 strike, used to trigger the
# "no matching contract" error path.
_NO_STRIKE_RESP = {
    "underlying": _CHAIN_RESP["underlying"],
    "callExpDateMap": {
        "2026-05-01:9": {
            "200.0": _CHAIN_RESP["callExpDateMap"]["2026-05-01:9"]["200.0"],
        },
    },
    "putExpDateMap": {},
}

# ---------------------------------------------------------------------------
# Golden string constants (captured from the current code)
# ---------------------------------------------------------------------------

# JSON golden output for the 202.5 CALL (parsed from repr captured above).
_GOLDEN_JSON_UNDERLYING_SYMBOL = "NVDA"
_GOLDEN_JSON_EXPIRY = "2026-05-01"
_GOLDEN_JSON_DTE = 9
_GOLDEN_JSON_CONTRACT_SYMBOL = "NVDA  260501C00202500"
_GOLDEN_JSON_CONTRACT_SIDE = "C"
_GOLDEN_JSON_CONTRACT_STRIKE = 202.5
_GOLDEN_JSON_CONTRACT_DELTA = 0.51
_GOLDEN_JSON_CONTRACT_BID = 4.7
_GOLDEN_JSON_CONTRACT_ASK = 4.8
_GOLDEN_JSON_CONTRACT_LAST = 4.75
_GOLDEN_JSON_CONTRACT_MARK = 4.75
_GOLDEN_JSON_CONTRACT_GAMMA = 0.035
_GOLDEN_JSON_CONTRACT_THETA = -0.267
_GOLDEN_JSON_CONTRACT_VEGA = 0.125
_GOLDEN_JSON_CONTRACT_RHO = 0.023
_GOLDEN_JSON_CONTRACT_VOLUME = 8809
_GOLDEN_JSON_CONTRACT_OI = 5174
_GOLDEN_JSON_CONTRACT_TIME_VALUE = 4.75
_GOLDEN_JSON_CONTRACT_INTRINSIC = 0.0
_GOLDEN_JSON_CONTRACT_ITM = False
_GOLDEN_JSON_CONTRACT_MULTIPLIER = 100
_GOLDEN_JSON_CONTRACT_SETTLE = "P"

# MD golden lines captured from current rendering.
_GOLDEN_MD_HEADING = "# NVDA 2026-05-01 CALL $202.50"
_GOLDEN_MD_CONTRACT_LINE = "**Contract:** `NVDA  260501C00202500`"
_GOLDEN_MD_EXPIRY_LINE = "**Expiry:** 2026-05-01 (9 DTE)"
_GOLDEN_MD_UNDERLYING_LINE = "**Underlying:** $202.50 (+2.62 / +1.31%)"
_GOLDEN_MD_QUOTE_SECTION = "## Quote"
_GOLDEN_MD_GREEKS_SECTION = "## Greeks"
_GOLDEN_MD_VALUE_SECTION = "## Value"
_GOLDEN_MD_QUOTE_HEADER = "| Field | Value |"
_GOLDEN_MD_QUOTE_SEP = "| --- | ---: |"
_GOLDEN_MD_BID_ROW = "| Bid | $4.70 |"
_GOLDEN_MD_ASK_ROW = "| Ask | $4.80 |"
_GOLDEN_MD_MID_ROW = "| Mid | $4.75 |"
_GOLDEN_MD_LAST_ROW = "| Last | $4.75 |"
_GOLDEN_MD_MARK_ROW = "| Mark | $4.75 |"
_GOLDEN_MD_VOLUME_ROW = "| Volume | 8,809 |"
_GOLDEN_MD_OI_ROW = "| Open Interest | 5,174 |"
_GOLDEN_MD_DELTA_ROW = "| Δ delta | 0.5100 |"
_GOLDEN_MD_GAMMA_ROW = "| Γ gamma | 0.0350 |"
_GOLDEN_MD_THETA_ROW = "| Θ theta (per day) | -0.2670 |"
_GOLDEN_MD_VEGA_ROW = "| 𝒱 vega (per 1% IV) | 0.1250 |"
_GOLDEN_MD_RHO_ROW = "| ρ rho (per 1% rate) | 0.0230 |"
_GOLDEN_MD_IV_ROW = "| IV | 36.58% |"
_GOLDEN_MD_INTRINSIC_ROW = "| Intrinsic | $0.00 |"
_GOLDEN_MD_EXTRINSIC_ROW = "| Extrinsic (time) | $4.75 |"
_GOLDEN_MD_BREAKEVEN_ROW = "| Break-even | $207.25 (+2.35% vs spot) |"
_GOLDEN_MD_ITM_ROW = "| In the money | no |"
_GOLDEN_MD_MULTIPLIER_ROW = "| Multiplier | 100 |"
_GOLDEN_MD_SETTLE_ROW = "| Settlement | P |"


# ---------------------------------------------------------------------------
# 1. Golden HUMAN output
# ---------------------------------------------------------------------------


def test_human_exit_code(monkeypatch, tmp_path):
    """Happy-path HUMAN output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 0, result.output


def test_human_contains_underlying_symbol(monkeypatch, tmp_path):
    """HUMAN output must contain the underlying symbol."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "NVDA" in result.output


def test_human_contains_expiry(monkeypatch, tmp_path):
    """HUMAN output must contain the expiry date."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "2026-05-01" in result.output


def test_human_contains_strike(monkeypatch, tmp_path):
    """HUMAN output must contain the formatted strike price."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "$202.50" in result.output


def test_human_contains_option_symbol(monkeypatch, tmp_path):
    """HUMAN output must contain the canonical OSI option symbol."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "NVDA  260501C00202500" in result.output


def test_human_contains_iv_formatted(monkeypatch, tmp_path):
    """HUMAN output must show IV as a percent with 2 decimal places."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "36.58%" in result.output


def test_human_contains_all_greek_glyphs(monkeypatch, tmp_path):
    """HUMAN output must render every Greek glyph (labels are load-bearing UX)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    for glyph in ("Δ", "Γ", "Θ", "𝒱", "ρ"):
        assert glyph in result.output, f"Missing greek glyph: {glyph!r}"


def test_human_contains_dte(monkeypatch, tmp_path):
    """HUMAN output must show DTE from the chain response."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "9 DTE" in result.output


def test_human_contains_breakeven(monkeypatch, tmp_path):
    """HUMAN output must show Break-even price and percent move vs spot."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    # break-even for this call = strike 202.50 + mark 4.75 = 207.25
    assert "$207.25" in result.output
    assert "+2.35% vs spot" in result.output


def test_human_contains_quote_section(monkeypatch, tmp_path):
    """HUMAN output must contain a Quote section with bid/ask/last values."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "Quote" in result.output
    assert "$4.70" in result.output  # bid
    assert "$4.80" in result.output  # ask
    assert "$4.75" in result.output  # last / mark / mid


def test_human_contains_value_decomposition(monkeypatch, tmp_path):
    """HUMAN output must contain intrinsic, extrinsic, and ITM status."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "Intrinsic" in result.output
    assert "Extrinsic" in result.output
    assert "In the money" in result.output


# ---------------------------------------------------------------------------
# 2. Golden JSON output — exact envelope/contract structure and values
# ---------------------------------------------------------------------------


def test_json_exit_code(monkeypatch, tmp_path):
    """JSON output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    assert result.exit_code == 0, result.output


def test_json_top_level_keys(monkeypatch, tmp_path):
    """JSON envelope must have exactly these top-level keys."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert set(data.keys()) == {
        "underlyingSymbol",
        "expiry",
        "dte",
        "underlying",
        "contract",
    }


def test_json_underlying_symbol_value(monkeypatch, tmp_path):
    """JSON ``underlyingSymbol`` must equal the ticker's underlying."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["underlyingSymbol"] == _GOLDEN_JSON_UNDERLYING_SYMBOL


def test_json_expiry_value(monkeypatch, tmp_path):
    """JSON ``expiry`` must be the ISO date string from the chain response."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["expiry"] == _GOLDEN_JSON_EXPIRY


def test_json_dte_value(monkeypatch, tmp_path):
    """JSON ``dte`` must be the integer from the chain response."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["dte"] == _GOLDEN_JSON_DTE


def test_json_underlying_block(monkeypatch, tmp_path):
    """JSON ``underlying`` block must contain last/netChange/pctChange."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    u = data["underlying"]
    assert u["last"] == 202.5
    assert u["netChange"] == 2.62
    assert u["pctChange"] == 1.31


def test_json_contract_option_symbol(monkeypatch, tmp_path):
    """JSON contract ``optionSymbol`` must be the canonical OSI form."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["contract"]["optionSymbol"] == _GOLDEN_JSON_CONTRACT_SYMBOL


def test_json_contract_side(monkeypatch, tmp_path):
    """JSON contract ``side`` must be 'C' for a call ticker."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["contract"]["side"] == _GOLDEN_JSON_CONTRACT_SIDE


def test_json_contract_strike(monkeypatch, tmp_path):
    """JSON contract ``strike`` must match the requested strike exactly."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    assert data["contract"]["strike"] == _GOLDEN_JSON_CONTRACT_STRIKE


def test_json_contract_greeks_values(monkeypatch, tmp_path):
    """JSON contract must contain exact golden greek values."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["delta"] == _GOLDEN_JSON_CONTRACT_DELTA
    assert c["gamma"] == _GOLDEN_JSON_CONTRACT_GAMMA
    assert c["theta"] == _GOLDEN_JSON_CONTRACT_THETA
    assert c["vega"] == _GOLDEN_JSON_CONTRACT_VEGA
    assert c["rho"] == _GOLDEN_JSON_CONTRACT_RHO


def test_json_contract_iv_is_fraction(monkeypatch, tmp_path):
    """JSON contract ``iv`` must be a fraction (0..1), not a percent."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    iv = data["contract"]["iv"]
    # volatility field in payload is 36.582 (%) -> iv = 36.582/100
    assert 0.35 < iv < 0.37


def test_json_contract_quote_fields(monkeypatch, tmp_path):
    """JSON contract quote fields must match the golden bid/ask/last/mark."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["bid"] == _GOLDEN_JSON_CONTRACT_BID
    assert c["ask"] == _GOLDEN_JSON_CONTRACT_ASK
    assert c["last"] == _GOLDEN_JSON_CONTRACT_LAST
    assert c["mark"] == _GOLDEN_JSON_CONTRACT_MARK


def test_json_contract_volume_and_oi(monkeypatch, tmp_path):
    """JSON contract volume and openInterest must be integer golden values."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["volume"] == _GOLDEN_JSON_CONTRACT_VOLUME
    assert c["openInterest"] == _GOLDEN_JSON_CONTRACT_OI


def test_json_contract_value_decomposition(monkeypatch, tmp_path):
    """JSON contract value fields must match golden intrinsic/timeValue/ITM."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["intrinsic"] == _GOLDEN_JSON_CONTRACT_INTRINSIC
    assert c["timeValue"] == _GOLDEN_JSON_CONTRACT_TIME_VALUE
    assert c["inTheMoney"] == _GOLDEN_JSON_CONTRACT_ITM
    assert c["multiplier"] == _GOLDEN_JSON_CONTRACT_MULTIPLIER
    assert c["settlementType"] == _GOLDEN_JSON_CONTRACT_SETTLE


def test_json_contract_null_optional_fields(monkeypatch, tmp_path):
    """Fields not in payload (bidSize, askSize, lastSize, open, high, low, close)
    must be null in JSON output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    data = json.loads(result.stdout)
    c = data["contract"]
    for field in ("bidSize", "askSize", "lastSize", "open", "high", "low", "close"):
        assert c[field] is None, f"Expected null for {field!r}, got {c[field]!r}"


# ---------------------------------------------------------------------------
# 3. Golden MD output — exact header/data lines
# ---------------------------------------------------------------------------


def test_md_exit_code(monkeypatch, tmp_path):
    """MD output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert result.exit_code == 0, result.output


def test_md_heading_line(monkeypatch, tmp_path):
    """MD output must start with the exact H1 heading (golden)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert _GOLDEN_MD_HEADING in result.stdout


def test_md_contract_line(monkeypatch, tmp_path):
    """MD output must contain the exact contract bold-backtick line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert _GOLDEN_MD_CONTRACT_LINE in result.stdout


def test_md_expiry_line(monkeypatch, tmp_path):
    """MD output must contain the exact expiry+DTE bold line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert _GOLDEN_MD_EXPIRY_LINE in result.stdout


def test_md_underlying_line(monkeypatch, tmp_path):
    """MD output must contain the exact underlying spot+change bold line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert _GOLDEN_MD_UNDERLYING_LINE in result.stdout


def test_md_section_headers(monkeypatch, tmp_path):
    """MD output must contain ## Quote, ## Greeks, ## Value section headers."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    assert _GOLDEN_MD_QUOTE_SECTION in result.stdout
    assert _GOLDEN_MD_GREEKS_SECTION in result.stdout
    assert _GOLDEN_MD_VALUE_SECTION in result.stdout


def test_md_quote_table_rows(monkeypatch, tmp_path):
    """MD quote table must contain exact golden bid/ask/mid/last/mark/volume/OI rows."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    for golden_row in (
        _GOLDEN_MD_BID_ROW,
        _GOLDEN_MD_ASK_ROW,
        _GOLDEN_MD_MID_ROW,
        _GOLDEN_MD_LAST_ROW,
        _GOLDEN_MD_MARK_ROW,
        _GOLDEN_MD_VOLUME_ROW,
        _GOLDEN_MD_OI_ROW,
    ):
        assert golden_row in result.stdout, f"Missing MD quote row: {golden_row!r}"


def test_md_greeks_table_rows(monkeypatch, tmp_path):
    """MD greeks table must contain exact golden delta/gamma/theta/vega/rho/IV rows."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    for golden_row in (
        _GOLDEN_MD_DELTA_ROW,
        _GOLDEN_MD_GAMMA_ROW,
        _GOLDEN_MD_THETA_ROW,
        _GOLDEN_MD_VEGA_ROW,
        _GOLDEN_MD_RHO_ROW,
        _GOLDEN_MD_IV_ROW,
    ):
        assert golden_row in result.stdout, f"Missing MD greeks row: {golden_row!r}"


def test_md_value_table_rows(monkeypatch, tmp_path):
    """MD value table must contain exact golden intrinsic/extrinsic/breakeven/ITM rows."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    for golden_row in (
        _GOLDEN_MD_INTRINSIC_ROW,
        _GOLDEN_MD_EXTRINSIC_ROW,
        _GOLDEN_MD_BREAKEVEN_ROW,
        _GOLDEN_MD_ITM_ROW,
        _GOLDEN_MD_MULTIPLIER_ROW,
        _GOLDEN_MD_SETTLE_ROW,
    ):
        assert golden_row in result.stdout, f"Missing MD value row: {golden_row!r}"


def test_md_table_header_and_separator_format(monkeypatch, tmp_path):
    """MD tables must use '| Field | Value |' header with right-aligned separator."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--md"])
    # Quote table header + separator (right-aligned value column)
    assert _GOLDEN_MD_QUOTE_HEADER in result.stdout
    assert _GOLDEN_MD_QUOTE_SEP in result.stdout


# ---------------------------------------------------------------------------
# 4. Ticker input-form tolerance — alternate forms resolve identically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker_form",
    [
        "NVDA260501C202.5",         # compact with decimal strike
        "NVDA260501C00202500",       # compact OSI form
        "NVDA  260501C00202500",     # Schwab-padded OSI form
    ],
)
def test_ticker_forms_resolve_same_contract(monkeypatch, tmp_path, ticker_form):
    """All three accepted ticker forms must return the same contract JSON."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", ticker_form, "--json"])
    assert result.exit_code == 0, f"{ticker_form}: {result.output}"
    data = json.loads(result.stdout)
    assert data["contract"]["optionSymbol"] == "NVDA  260501C00202500"
    assert data["contract"]["strike"] == 202.5
    assert data["contract"]["side"] == "C"


# ---------------------------------------------------------------------------
# 5. _pick_contract exactness — correct strike/side selected from multi-strike payload
# ---------------------------------------------------------------------------


def test_pick_contract_selects_202_5_not_200(monkeypatch, tmp_path):
    """_pick_contract must select the 202.5 call, ignoring the 200.0 call in payload."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["strike"] == 202.5
    assert c["optionSymbol"] == "NVDA  260501C00202500"
    assert c["delta"] == 0.51        # golden delta for the 202.5 contract
    # Confirm the 200.0 contract was NOT selected (its delta is 0.595)
    assert c["delta"] != 0.595


def test_pick_contract_put_side_from_put_only_payload(monkeypatch, tmp_path):
    """A PUT ticker must select only the put contract, not the call side."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_PUT_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501P202.5", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    c = data["contract"]
    assert c["side"] == "P"
    assert c["strike"] == 202.5
    assert c["optionSymbol"] == "NVDA  260501P00202500"
    assert c["delta"] == -0.489


def test_pick_contract_put_json_structure(monkeypatch, tmp_path):
    """PUT contract JSON envelope must have the same top-level structure as CALL."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_PUT_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501P202.5", "--json"])
    data = json.loads(result.stdout)
    assert set(data.keys()) == {
        "underlyingSymbol",
        "expiry",
        "dte",
        "underlying",
        "contract",
    }
    assert data["underlyingSymbol"] == "NVDA"


# ---------------------------------------------------------------------------
# 6. Error/exit codes
# ---------------------------------------------------------------------------


def test_error_stock_ticker_exit_2(monkeypatch, tmp_path):
    """A plain stock ticker (e.g. 'AAPL') must exit 2."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "AAPL"])
    assert result.exit_code == 2


def test_error_stock_ticker_message(monkeypatch, tmp_path):
    """A plain stock ticker must print 'is not an option ticker'."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "AAPL"])
    assert "is not an option ticker" in result.output


def test_error_unparseable_ticker_exit_2(monkeypatch, tmp_path):
    """An unparseable ticker (bad separator) must exit 2."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "NVDA-260501C240"])
    assert result.exit_code == 2


def test_error_unparseable_ticker_message(monkeypatch, tmp_path):
    """An unparseable ticker must print 'unrecognized ticker' (case-insensitive)."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["greeks", "NVDA-260501C240"])
    assert "unrecognized ticker" in result.output.lower()


def test_error_both_flags_exit_2(monkeypatch, tmp_path):
    """--json and --md together must exit 2."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json", "--md"])
    assert result.exit_code == 2


def test_error_both_flags_message(monkeypatch, tmp_path):
    """--json and --md together must print 'mutually exclusive'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5", "--json", "--md"])
    assert "mutually exclusive" in result.output


def test_error_no_config_exit_1(monkeypatch, tmp_path):
    """Missing config must exit 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # tmp_path is empty — no config, no session
    result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1


def test_error_no_config_message(monkeypatch, tmp_path):
    """Missing config must print 'No config'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "No config" in result.output


def test_error_no_session_exit_1(monkeypatch, tmp_path):
    """Config present but missing session must exit 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    # No session saved
    result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1


def test_error_no_session_message(monkeypatch, tmp_path):
    """Missing session must print 'No session'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "No session" in result.output


def test_error_no_matching_contract_exit_1(monkeypatch, tmp_path):
    """No matching strike/side in chain response must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_NO_STRIKE_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1


def test_error_no_matching_contract_message(monkeypatch, tmp_path):
    """No matching contract must print 'No CALL contract' and the strike."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_NO_STRIKE_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "No CALL contract" in result.output
    assert "202.50" in result.output


def test_error_no_matching_put_contract_message(monkeypatch, tmp_path):
    """No matching PUT contract must say 'No PUT contract' in the message."""
    _prep(monkeypatch, tmp_path)
    # Use the call-only payload but request a PUT
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_NO_STRIKE_RESP):
        result = runner.invoke(app, ["greeks", "NVDA260501P202.5"])
    assert result.exit_code == 1
    assert "No PUT contract" in result.output


def test_error_session_expired_exit_1(monkeypatch, tmp_path):
    """SessionExpired raised from SchwabClient.get must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("token expired"),
    ):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1


def test_error_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired must surface the exception message in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("token expired"),
    ):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "token expired" in result.output


def test_error_api_error_exit_1(monkeypatch, tmp_path):
    """ApiError raised from SchwabClient.get must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert result.exit_code == 1


def test_error_api_error_message(monkeypatch, tmp_path):
    """ApiError message must appear in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["greeks", "NVDA260501C202.5"])
    assert "503" in result.output
