"""Command-level tests for ``schwab_cli vol``.

The command makes two API calls (chain + price history). Both are mocked
here so the tests stay offline and deterministic. We verify the
command's glue — correct parameters to each API, correct envelope
assembly, and the three output formats render without error.
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
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


# ---- synthetic API responses -------------------------------------------


# Short chain: one near-dated expiry with three strikes. Volume on one
# strike across both legs is ≥ 100 so the ATM picker accepts it.
_CHAIN_RESP = {
    "symbol": "NVDA",
    "underlying": {"last": 202.50, "change": 2.62, "percentChange": 1.31},
    "callExpDateMap": {
        "2026-05-01:9": {
            "200.0": [{
                "putCall": "CALL", "strikePrice": 200.0, "volatility": 35.0,
                "totalVolume": 500, "openInterest": 300,
            }],
            "202.5": [{
                "putCall": "CALL", "strikePrice": 202.5, "volatility": 36.58,
                "totalVolume": 1000, "openInterest": 500,
            }],
            "205.0": [{
                "putCall": "CALL", "strikePrice": 205.0, "volatility": 37.5,
                "totalVolume": 200, "openInterest": 150,
            }],
        }
    },
    "putExpDateMap": {
        "2026-05-01:9": {
            "200.0": [{
                "putCall": "PUT", "strikePrice": 200.0, "volatility": 37.0,
                "totalVolume": 300, "openInterest": 200,
            }],
            "202.5": [{
                "putCall": "PUT", "strikePrice": 202.5, "volatility": 36.58,
                "totalVolume": 720, "openInterest": 470,
            }],
            "205.0": [{
                "putCall": "PUT", "strikePrice": 205.0, "volatility": 38.0,
                "totalVolume": 200, "openInterest": 150,
            }],
        }
    },
}


def _history_resp(n_days: int) -> dict:
    """Deterministic synthetic 1-day candles.

    Prices oscillate ±$1 so log returns are computable and HV is non-zero.
    """
    return {
        "symbol": "NVDA",
        "candles": [
            {
                "datetime": i * 86_400_000,
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.0 + (1.0 if i % 2 == 0 else -1.0),
                "volume": 1_000_000,
            }
            for i in range(n_days)
        ],
    }


# ---- tests --------------------------------------------------------------


def test_vol_happy_path_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA"])
    assert result.exit_code == 0, result.output

    # Header carries symbol + spot.
    assert "NVDA" in result.output
    assert "$202.50" in result.output
    # Every row label appears.
    for label in ("IV", "HV", "HVP", "P/C vol", "P/C OI", "IVP"):
        assert label in result.output
    # IV value derived from midpoint of call/put at 202.5 strike.
    # Midpoint is 0.3658 → rendered as 36.58%.
    assert "36.58%" in result.output
    # IVP is the phase-1 placeholder.
    assert "not yet active" in result.output


def test_vol_json_shape_and_values(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--json"])
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)

    assert env["symbol"] == "NVDA"
    assert env["spot"] == 202.50

    # ATM pick — closest strike to spot with sufficient volume.
    assert env["iv"]["strike"] == 202.5
    assert env["iv"]["expiry"] == "2026-05-01"
    assert env["iv"]["dte"] == 9
    assert abs(env["iv"]["value"] - 0.3658) < 1e-3

    # HV computed from the synthetic oscillating series.
    assert env["hv"]["window"] == 30
    assert env["hv"]["value"] is not None
    assert env["hv"]["value"] > 0

    # HVP has a sample of rolling values. Since the input series is
    # perfectly alternating, the rolling values vary slightly but are
    # all well-defined.
    assert env["hvp"]["value"] is not None
    assert 0 <= env["hvp"]["value"] <= 100

    # P/C ratios: sum(put_vol=300+720+200=1220) / sum(call_vol=500+1000+200=1700) ≈ 0.718
    assert abs(env["pc"]["volume_ratio"] - (1220 / 1700)) < 1e-9
    # OI: 820/950 ≈ 0.863
    assert abs(env["pc"]["oi_ratio"] - (820 / 950)) < 1e-9

    # IVP is explicit placeholder.
    assert env["ivp"]["state"] == "not_yet_active"
    assert env["ivp"]["value"] is None


def test_vol_md_has_all_rows(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--md"])
    assert result.exit_code == 0, result.output
    for label in ("| IV ", "| HV ", "| HVP ", "| P/C vol ", "| P/C OI ", "| IVP "):
        assert label in result.output


def test_vol_rejects_option_ticker(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["vol", "NVDA260501C240"])
    assert result.exit_code == 2
    assert "stock" in result.output.lower()


def test_vol_missing_spot_exits_1(monkeypatch, tmp_path):
    """If the chain response lacks underlying.last, the command bails cleanly."""
    _prep(monkeypatch, tmp_path)
    resp = {**_CHAIN_RESP, "underlying": {}}
    with patch("schwab_cli.commands.vol.get_chain", return_value=resp), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA"])
    assert result.exit_code == 1
    assert "spot" in result.output.lower()


def test_vol_hv_none_when_history_too_short(monkeypatch, tmp_path):
    """Fewer closes than the HV window means HV and HVP are both None."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(10)):
        result = runner.invoke(app, ["vol", "NVDA", "--json"])
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["hv"]["value"] is None
    assert env["hvp"]["value"] is None
    assert env["hvp"]["sample_size"] == 0


def test_vol_chain_call_uses_wide_params(monkeypatch, tmp_path):
    """Chain call should use ALL contract type and strike_count=60."""
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_chain(client, symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return _CHAIN_RESP

    with patch("schwab_cli.commands.vol.get_chain", side_effect=fake_chain), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        runner.invoke(app, ["vol", "NVDA"])

    assert captured["symbol"] == "NVDA"
    assert captured["contract_type"] == "ALL"
    assert captured["strike_count"] == 60
    assert captured["from_date"] is not None
    assert captured["to_date"] is not None
