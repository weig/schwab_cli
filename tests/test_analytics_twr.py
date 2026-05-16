"""TWR pure-math tests."""
from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics import twr


def _navs(*tuples):
    return [
        twr.DailyNav(day=date(2026, 1, d), value=v, external_flow=f)
        for d, v, f in tuples
    ]


def test_chain_link_zero_for_empty_or_single_point():
    assert twr.chain_link([]) == 0.0
    assert twr.chain_link(_navs((1, 100.0, 0.0))) == 0.0


def test_chain_link_pure_appreciation():
    # +10% per day for 2 days → 21%
    navs = _navs((1, 100.0, 0.0), (2, 110.0, 0.0), (3, 121.0, 0.0))
    assert twr.chain_link(navs) == pytest.approx(0.21, rel=1e-6)


def test_chain_link_neutralizes_mid_period_deposit():
    """Deposit $10 on day 2 with otherwise flat market → TWR ≈ 0."""
    # Day 1: $100. Day 2: $110, but $10 was deposited (CF=+10). Real perf 0%.
    navs = _navs((1, 100.0, 0.0), (2, 110.0, 10.0))
    assert twr.chain_link(navs) == pytest.approx(0.0, abs=1e-9)


def test_chain_link_neutralizes_withdrawal():
    """Withdraw $10 on day 2 with flat market → TWR ≈ 0."""
    navs = _navs((1, 100.0, 0.0), (2, 90.0, -10.0))
    assert twr.chain_link(navs) == pytest.approx(0.0, abs=1e-9)


def test_chain_link_skips_zero_begin_value():
    """Funding an empty account shouldn't crash with /0."""
    navs = _navs((1, 0.0, 0.0), (2, 100.0, 100.0), (3, 110.0, 0.0))
    assert twr.chain_link(navs) == pytest.approx(0.10, rel=1e-6)


def test_simple_return():
    assert twr.simple_return(100.0, 110.0) == pytest.approx(0.10)
    assert twr.simple_return(100.0, 90.0) == pytest.approx(-0.10)
    assert twr.simple_return(0.0, 110.0) == 0.0  # safety: no /0


# ---- transaction parsing ---------------------------------------------


def _tx(time_iso, type_, net, *legs):
    """legs: (symbol, qty, asset_type)"""
    items = [
        {
            "amount": qty,
            "instrument": {"symbol": sym, "assetType": at},
            "feeType": None,
        }
        for sym, qty, at in legs
    ]
    return {"time": time_iso, "type": type_, "netAmount": net,
            "transferItems": items}


def test_parse_transaction_buy_records_position_and_cash():
    raw = _tx("2026-03-01T15:00:00+0000", "TRADE", -1000.0,
              ("NVDA", 10, "EQUITY"))
    d = twr.parse_transaction(raw)
    assert d is not None
    assert d.cash_delta == -1000.0
    assert d.position_deltas == {"NVDA": 10.0}
    assert d.is_external_flow is False


def test_parse_transaction_external_flow_marks_pure_cash_journal():
    raw = {"time": "2026-03-01T15:00:00+0000", "type": "JOURNAL",
           "netAmount": 5000.0, "transferItems": []}
    d = twr.parse_transaction(raw)
    assert d.is_external_flow is True
    assert d.cash_delta == 5000.0
    assert d.position_deltas == {}


def test_parse_transaction_receive_and_deliver_with_security_is_internal():
    """Option exercise/assignment shows up as RECEIVE_AND_DELIVER but
    has security legs — it's an internal portfolio event, not a flow."""
    raw = _tx("2026-03-01T15:00:00+0000", "RECEIVE_AND_DELIVER", 0.0,
              ("NVDA", 100, "EQUITY"))
    d = twr.parse_transaction(raw)
    assert d.is_external_flow is False


def test_parse_transaction_pure_cash_acats_is_external():
    raw = {"time": "2026-03-01T15:00:00+0000", "type": "RECEIVE_AND_DELIVER",
           "netAmount": 10000.0, "transferItems": []}
    d = twr.parse_transaction(raw)
    assert d.is_external_flow is True


def test_parse_transaction_skips_currency_leg():
    raw = _tx("2026-03-01T15:00:00+0000", "TRADE", 200.0,
              ("CURRENCY_USD", 200, "CURRENCY"),
              ("AAPL", -1, "EQUITY"))
    d = twr.parse_transaction(raw)
    # Currency leg ignored; only AAPL goes into position_deltas.
    assert d.position_deltas == {"AAPL": -1.0}


def test_parse_transaction_returns_none_for_missing_time():
    assert twr.parse_transaction({"type": "TRADE"}) is None


# ---- reconstruct history ---------------------------------------------


def test_reconstruct_walks_back_through_a_buy():
    """Today we hold 10 NVDA + $5k cash. Yesterday we bought 10 NVDA
    for $5k. Therefore the day before the buy we had 0 NVDA + $10k."""
    today = date(2026, 3, 2)
    txns = [_tx("2026-03-01T15:00:00+0000", "TRADE", -5000.0,
                ("NVDA", 10, "EQUITY"))]
    days = [date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 2)]
    states = twr.reconstruct_history(
        today=today,
        today_positions={"NVDA": 10.0},
        today_cash=5000.0,
        transactions=txns,
        days=days,
    )
    by_day = {s.day: s for s in states}
    assert by_day[today].positions == {"NVDA": 10.0}
    assert by_day[today].cash == 5000.0
    # End of buy-day already shows the trade (transactions on day D
    # are part of day-D state).
    assert by_day[date(2026, 3, 1)].positions == {"NVDA": 10.0}
    # Day before: position not yet bought.
    assert by_day[date(2026, 2, 28)].positions == {}
    assert by_day[date(2026, 2, 28)].cash == pytest.approx(10000.0)


def _tx_with_effect(time_iso, qty, cost, effect, sym="NVDA"):
    return {
        "time": time_iso,
        "type": "TRADE",
        "netAmount": cost,
        "transferItems": [{
            "amount": qty, "cost": cost,
            "positionEffect": effect, "feeType": None,
            "instrument": {"symbol": sym, "assetType": "EQUITY"},
        }],
    }


def test_realized_fifo_long_round_trip():
    txns = [
        _tx_with_effect("2026-02-01T15:00:00+0000", 100, -10000, "OPENING"),
        _tx_with_effect("2026-03-01T15:00:00+0000", -100, 11000, "CLOSING"),
    ]
    assert twr.realized_pl_fifo(txns) == pytest.approx(1000.0)


def test_realized_fifo_short_round_trip():
    txns = [
        _tx_with_effect("2026-02-01T15:00:00+0000", -100, 10000, "OPENING"),
        _tx_with_effect("2026-03-01T15:00:00+0000", 100, -9000, "CLOSING"),
    ]
    assert twr.realized_pl_fifo(txns) == pytest.approx(1000.0)


def test_realized_fifo_skips_orphan_closes():
    txns = [
        _tx_with_effect("2026-03-01T15:00:00+0000", -100, 11000, "CLOSING"),
    ]
    assert twr.realized_pl_fifo(txns) == 0.0


def test_classify_transactions_buckets_correctly():
    txns = [
        {"time": "2026-02-01T15:00:00+0000", "type": "JOURNAL",
         "netAmount": 5000.0, "transferItems": []},
        {"time": "2026-02-05T15:00:00+0000", "type": "JOURNAL",
         "netAmount": -2000.0, "transferItems": []},
        {"time": "2026-02-15T15:00:00+0000", "type": "DIVIDEND_OR_INTEREST",
         "netAmount": 97.0, "transferItems": []},
        {"time": "2026-03-02T15:00:00+0000", "type": "TRADE",
         "netAmount": 990.0,
         "transferItems": [
             {"amount": 10, "cost": -1000, "positionEffect": "OPENING",
              "feeType": None,
              "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}},
             {"cost": -10.0, "feeType": "COMMISSION"},
         ]},
    ]
    out = twr.classify_transactions(txns)
    assert out["inflow"] == pytest.approx(5000.0)
    assert out["outflow"] == pytest.approx(-2000.0)
    assert out["income"] == pytest.approx(97.0)
    assert out["fees"] == pytest.approx(-10.0)


def test_reconstruct_records_external_flow_on_day_of_deposit():
    today = date(2026, 3, 2)
    txns = [{"time": "2026-03-01T15:00:00+0000", "type": "JOURNAL",
             "netAmount": 1000.0, "transferItems": []}]
    states = twr.reconstruct_history(
        today=today,
        today_positions={},
        today_cash=2000.0,
        transactions=txns,
        days=[date(2026, 2, 28), date(2026, 3, 1), today],
    )
    by_day = {s.day: s for s in states}
    assert by_day[date(2026, 3, 1)].external_flow == pytest.approx(1000.0)
    # Day before deposit: only $1000 in the account.
    assert by_day[date(2026, 2, 28)].cash == pytest.approx(1000.0)
