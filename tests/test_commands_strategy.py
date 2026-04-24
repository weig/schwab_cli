"""Command-level tests for ``schwab_cli strategy``.

Network is mocked via ``patch("schwab_cli.commands.strategy.get_chain")``
so tests stay offline. Each mode-under-test pins down:

* CLI argument parsing for ``--leg`` repeats.
* Chain fetch happens once per unique expiry.
* Envelope handed to the renderer carries the analytics + ticket +
  warnings.
* HUMAN / JSON outputs render without error and carry the anchors
  downstream code depends on.
* Usage errors exit 2; runtime failures exit 1.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, save as save_config
from schwab_cli.session import Session, save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


def _future_dates() -> tuple[str, str, int]:
    """Return (YYYYMMDD, ISO, DTE) for a date ~30 days out."""
    d = date.today() + timedelta(days=30)
    return d.strftime("%Y%m%d"), d.isoformat(), 30


def _chain_resp(iso: str, *, symbol: str = "AMZN", dte: int = 30) -> dict:
    """Chain response dense enough to price vertical + IC + fly legs.

    Strikes span 240-280 for calls and 220-260 for puts; each carries
    a reasonable delta, IV, and bid/ask.
    """
    calls = {
        245.0: (0.75, 0.35, 12.50),
        250.0: (0.65, 0.33, 8.00),
        255.0: (0.52, 0.31, 4.50),
        260.0: (0.38, 0.30, 2.00),
        265.0: (0.26, 0.29, 0.95),
        270.0: (0.17, 0.28, 0.45),
        275.0: (0.10, 0.28, 0.20),
        280.0: (0.06, 0.28, 0.10),
    }
    puts = {
        220.0: (-0.06, 0.32, 0.10),
        225.0: (-0.10, 0.31, 0.25),
        230.0: (-0.17, 0.30, 0.60),
        235.0: (-0.26, 0.30, 1.15),
        240.0: (-0.38, 0.29, 2.30),
        245.0: (-0.52, 0.30, 4.20),
        250.0: (-0.65, 0.31, 7.50),
        255.0: (-0.75, 0.32, 11.00),
        260.0: (-0.84, 0.33, 16.00),
    }

    def _row(strike: float, delta: float, iv: float, last: float, is_call: bool) -> dict:
        return {
            "symbol": f"{symbol}  {'C' if is_call else 'P'}{strike:08.0f}",
            "putCall": "CALL" if is_call else "PUT",
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,
            "bid": max(0.01, last - 0.05),
            "ask": last + 0.05,
            "last": last,
            "mark": last,
            "gamma": 0.02,
            "theta": -0.05,
            "vega": 0.15,
            "totalVolume": 100,
            "openInterest": 100,
            "expirationDate": iso,
            "daysToExpiration": dte,
        }

    return {
        "symbol": symbol,
        "underlying": {"last": 255.0, "change": 0.0, "percentChange": 0.0},
        "callExpDateMap": {
            f"{iso}:{dte}": {
                f"{s:.1f}": [_row(s, d, iv, last, True)]
                for s, (d, iv, last) in calls.items()
            }
        },
        "putExpDateMap": {
            f"{iso}:{dte}": {
                f"{s:.1f}": [_row(s, d, iv, last, False)]
                for s, (d, iv, last) in puts.items()
            }
        },
    }


# ---- happy path: vertical ---------------------------------------------


def test_strategy_bull_call_spread_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso, _ = _future_dates()
    with patch(
        "schwab_cli.commands.strategy.get_chain",
        return_value=_chain_resp(iso),
    ) as mock:
        result = runner.invoke(
            app,
            ["strategy", "AMZN",
             "--leg", f"+1@{yymmdd}C255",
             "--leg", f"-1@{yymmdd}C260"],
        )
    assert result.exit_code == 0, result.output
    assert "Bull Call Spread" in result.output
    assert "Schwab order ticket" in result.output
    assert "VERTICAL" in result.output
    assert "POP" in result.output
    # Net debit since +C255 (4.50) - -C260 (2.00) = 2.50 debit.
    assert "Net Debit" in result.output
    mock.assert_called_once()


def test_strategy_vertical_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso, _ = _future_dates()
    with patch(
        "schwab_cli.commands.strategy.get_chain",
        return_value=_chain_resp(iso),
    ):
        result = runner.invoke(
            app,
            ["strategy", "AMZN",
             "--leg", f"+1@{yymmdd}C255",
             "--leg", f"-1@{yymmdd}C260",
             "--json"],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["strategy"] == "Bull Call Spread"
    assert data["ticket_name"] == "VERTICAL"
    assert data["ticket"].startswith("BUY +1 VERTICAL AMZN 100")
    assert data["pop"] is not None
    assert data["max_profit"] is not None
    assert data["max_loss"] is not None
    assert data["net_debit"] > 0
    assert data["net_credit"] == 0


def test_strategy_iron_condor_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso, _ = _future_dates()
    with patch(
        "schwab_cli.commands.strategy.get_chain",
        return_value=_chain_resp(iso),
    ):
        result = runner.invoke(
            app,
            ["strategy", "AMZN",
             "--leg", f"+1@{yymmdd}P235",
             "--leg", f"-1@{yymmdd}P240",
             "--leg", f"-1@{yymmdd}C265",
             "--leg", f"+1@{yymmdd}C270",
             "--json"],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["strategy"] == "Iron Condor"
    assert data["ticket_name"] == "IRON CONDOR"
    assert data["ticket"].startswith("SELL -1 IRON CONDOR AMZN 100")
    assert "prob_touch_approx" in data["warnings"]
    assert len(data["breakevens"]) == 2


# ---- multi-expiry: calendar unsupported but ticket renders -----------


def test_strategy_calendar_renders_ticket_but_marks_unsupported(
    monkeypatch, tmp_path
):
    _prep(monkeypatch, tmp_path)
    d1 = date.today() + timedelta(days=30)
    d2 = date.today() + timedelta(days=90)
    yymmdd1 = d1.strftime("%Y%m%d")
    yymmdd2 = d2.strftime("%Y%m%d")
    iso1 = d1.isoformat()
    iso2 = d2.isoformat()

    # Far-dated chain carries fatter premiums so the net is a debit (long
    # calendar = pay time premium on the long leg).
    def _chain_far(iso: str, dte: int) -> dict:
        base = _chain_resp(iso, dte=dte)
        # Double every option's premium on the far-dated chain.
        for map_key in ("callExpDateMap", "putExpDateMap"):
            for strike_map in base[map_key].values():
                for strike_list in strike_map.values():
                    for row in strike_list:
                        row["last"] *= 2
                        row["mark"] *= 2
                        row["bid"] *= 2
                        row["ask"] *= 2
        return base

    def fake_fetch(*args, **kwargs):
        frm = kwargs.get("from_date")
        if frm == d1:
            return _chain_resp(iso1, dte=30)
        return _chain_far(iso2, dte=90)

    with patch(
        "schwab_cli.commands.strategy.get_chain",
        side_effect=fake_fetch,
    ):
        result = runner.invoke(
            app,
            ["strategy", "AMZN",
             # 270 is in the fixture chain — avoid strike-snap on a
             # strike that doesn't exist in the test chain.
             "--leg", f"-1@{yymmdd1}C270",
             "--leg", f"+1@{yymmdd2}C270",
             "--json"],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["supported"] is False
    assert data["reason"] == "multi-expiry"
    assert data["pop"] is None
    assert data["ticket_name"] == "CALENDAR"
    assert "CALENDAR" in data["ticket"]
    assert "analytics_not_supported_yet:multi-expiry" in data["warnings"]


# ---- strike snap warning ---------------------------------------------


def test_strategy_strike_snap_warning_emitted_for_off_strike(
    monkeypatch, tmp_path
):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso, _ = _future_dates()
    with patch(
        "schwab_cli.commands.strategy.get_chain",
        return_value=_chain_resp(iso),
    ):
        # 256.25 is not in the chain — nearest is 255 or 260, diff > 0.50.
        result = runner.invoke(
            app,
            ["strategy", "AMZN",
             "--leg", f"+1@{yymmdd}C256.25",
             "--json"],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    snaps = [w for w in data["warnings"] if w.startswith("strike_snap")]
    assert snaps, f"expected strike_snap warning, got {data['warnings']}"


# ---- usage errors ----------------------------------------------------


def test_strategy_missing_legs_exits_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["strategy", "AMZN"])
    # typer error (missing required --leg) exits with code 2.
    assert result.exit_code == 2


def test_strategy_bad_leg_syntax_exits_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        ["strategy", "AMZN", "--leg", "not-a-leg"],
    )
    assert result.exit_code == 2


def test_strategy_json_and_md_mutually_exclusive(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, _, _ = _future_dates()
    result = runner.invoke(
        app,
        ["strategy", "AMZN",
         "--leg", f"+1@{yymmdd}C255",
         "--json", "--md"],
    )
    assert result.exit_code == 2


# ---- chain fetch failure is fatal ------------------------------------


def test_strategy_chain_fetch_failure_exits_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, _, _ = _future_dates()
    from schwab_cli.api.client import ApiError

    with patch(
        "schwab_cli.commands.strategy.get_chain",
        side_effect=ApiError("upstream 500"),
    ):
        result = runner.invoke(
            app,
            ["strategy", "AMZN", "--leg", f"+1@{yymmdd}C255"],
        )
    assert result.exit_code == 1
    assert "chain fetch failed" in result.stderr or "chain fetch failed" in result.output
