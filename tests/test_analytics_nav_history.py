"""NAV history reconstruction + pricing."""
from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics import nav_history


def test_snapshot_from_payload_sums_position_market_values():
    sec = {
        "currentBalances": {"cashBalance": 1234.56},
        "positions": [
            {"marketValue": 5000.0},
            {"marketValue": 1500.0},
            {"marketValue": None},  # ignored
        ],
    }
    nav = nav_history.snapshot_today_from_payload(sec)
    assert nav.cash == 1234.56
    assert nav.market_value == 6500.0
    assert nav.estimated is False


def test_backfill_equity_pure_lookup_is_exact():
    """All-equity day: priced from ohlcv_daily closes only, no BS, no
    estimation flag."""
    nav = nav_history.backfill_day(
        day=date(2026, 2, 1),
        today=date(2026, 5, 15),
        today_cash=1000.0,
        today_positions={"NVDA": 10.0},
        transactions=[],  # no trades — held throughout
        equity_close={"NVDA": {date(2026, 2, 1): 130.0}},
        underlying_close={},
        atm_iv={},
    )
    assert nav.cash == 1000.0
    assert nav.market_value == 10 * 130.0
    assert nav.estimated is False


def test_backfill_option_uses_bs_and_flags_estimated():
    """Option position priced via Black-Scholes is flagged estimated."""
    osi = "NVDA  260601C00130000"   # 130 strike call, 2026-06-01
    nav = nav_history.backfill_day(
        day=date(2026, 2, 1),
        today=date(2026, 5, 15),
        today_cash=0.0,
        today_positions={osi: 1.0},
        transactions=[],
        equity_close={},
        underlying_close={"NVDA": {date(2026, 2, 1): 130.0}},
        atm_iv={"NVDA": {date(2026, 2, 1): 0.40}},
    )
    # At-the-money 4-month call with 40 vol: roughly $11–$12 per share
    # → ~$1100 per contract. Sanity-bound the range.
    assert 800 < nav.market_value < 2000
    assert nav.estimated is True


def test_backfill_option_falls_back_to_cost_basis_when_iv_missing():
    osi = "NVDA  260601C00130000"
    nav = nav_history.backfill_day(
        day=date(2026, 2, 1),
        today=date(2026, 5, 15),
        today_cash=0.0,
        today_positions={osi: 2.0},
        transactions=[],
        equity_close={},
        underlying_close={},
        atm_iv={},
        avg_price={osi: 5.0},
    )
    # Cost basis $5/share × 100 multiplier × 2 contracts = $1,000
    assert nav.market_value == 1000.0
    assert nav.estimated is True


def test_backfill_walks_back_through_purchase():
    """Bought 10 NVDA between target_day and today → position should
    not appear on target_day."""
    buy = {
        "time": "2026-03-01T15:00:00+0000",
        "type": "TRADE",
        "netAmount": -1000.0,
        "transferItems": [{
            "amount": 10, "cost": -1000,
            "positionEffect": "OPENING", "feeType": None,
            "instrument": {"symbol": "NVDA", "assetType": "EQUITY"},
        }],
    }
    nav = nav_history.backfill_day(
        day=date(2026, 2, 1),
        today=date(2026, 5, 15),
        today_cash=0.0,
        today_positions={"NVDA": 10.0},
        transactions=[buy],
        equity_close={"NVDA": {date(2026, 2, 1): 130.0}},
        underlying_close={}, atm_iv={},
    )
    # On Feb 1 (before the buy), positions were empty + cash $1000.
    assert nav.market_value == 0.0
    assert nav.cash == pytest.approx(1000.0)
    assert nav.estimated is False
