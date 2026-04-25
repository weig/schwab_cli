"""Phase 2c field provider tests — counters + position state."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from schwab_cli.order_policy.conditions import UnevaluatableField
from schwab_cli.order_policy.counters import Counters
from schwab_cli.order_policy.fields import FieldProvider, OrderContext


def _option_short_call_body(strike="00250000", expiry="260117", qty=1):
    sym = f"NVDA  {expiry}C{strike}"
    return {
        "orderType": "LIMIT", "price": "1.00", "quantity": qty,
        "complexOrderStrategyType": "NONE",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN", "quantity": qty,
            "instrument": {"assetType": "OPTION", "symbol": sym},
        }],
    }


def _equity_body(symbol="NVDA", qty=10, price="150.00", side="BUY"):
    return {
        "orderType": "LIMIT", "price": price, "quantity": qty,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": side, "quantity": qty,
            "instrument": {"assetType": "EQUITY", "symbol": symbol},
        }],
    }


def _account(*, positions=None, cash=50000.0, net_liq=100000.0):
    return {"securitiesAccount": {
        "currentBalances": {
            "liquidationValue": net_liq, "cashBalance": cash,
            "buyingPower": 70000.0, "longMarketValue": 5000.0,
            "shortMarketValue": 0.0, "maintenanceRequirement": 0.0,
        },
        "positions": positions or [],
    }}


def _equity_position(symbol="NVDA", qty=200, market_value=49000.0):
    return {
        "longQuantity": qty, "shortQuantity": 0,
        "marketValue": market_value,
        "instrument": {"assetType": "EQUITY", "symbol": symbol},
    }


def _option_position(symbol, *, long_qty=0, short_qty=0, market_value=0.0):
    return {
        "longQuantity": long_qty, "shortQuantity": short_qty,
        "marketValue": market_value,
        "instrument": {"assetType": "OPTION", "symbol": symbol},
    }


def _ctx(*, body, **slots):
    return OrderContext(
        body=body,
        account_number="12345678",
        today=date(2026, 4, 25),
        **slots,
    )


# ---- counter fields ------------------------------------------------------


def test_daily_order_count_reads_from_counters_data():
    counters = Counters(et_date="2026-04-25")
    counters.daily_total["12345678"] = 5
    counters.daily_per_ticker["12345678"] = {"NVDA": 3}
    p = FieldProvider(_ctx(body=_equity_body(), counters_data=counters))
    assert p.get("daily_order_count") == 5
    assert p.get("daily_order_count_per_ticker") == 3


def test_minutely_order_count_sums_recent_buckets():
    counters = Counters(et_date="2026-04-25")
    counters.minutely_buckets["12345678"] = {
        "2026-04-25T15:30": 2,
        "2026-04-25T15:31": 1,
    }
    p = FieldProvider(_ctx(
        body=_equity_body(), counters_data=counters,
        now_et=datetime(2026, 4, 25, 15, 31, tzinfo=timezone.utc),
    ))
    assert p.get("minutely_order_count") == 3


def test_replace_count_zero_when_no_order_id():
    counters = Counters(et_date="2026-04-25")
    p = FieldProvider(_ctx(body=_equity_body(), counters_data=counters))
    assert p.get("replace_count") == 0


def test_counter_field_unevaluatable_when_no_counters_data():
    p = FieldProvider(_ctx(body=_equity_body()))
    with pytest.raises(UnevaluatableField, match="counters_data"):
        p.get("daily_order_count")


# ---- consecutive_losing_closes_24h --------------------------------------


def test_consecutive_losing_closes_24h_streak_from_most_recent():
    body = _equity_body()
    txns = [
        {"type": "TRADE", "time": "2026-04-25T14:00:00+00:00", "netAmount": -50.0},
        {"type": "TRADE", "time": "2026-04-25T13:00:00+00:00", "netAmount": -25.0},
        # Older win — breaks the streak.
        {"type": "TRADE", "time": "2026-04-25T12:00:00+00:00", "netAmount": +75.0},
        {"type": "TRADE", "time": "2026-04-25T11:00:00+00:00", "netAmount": -10.0},
    ]
    p = FieldProvider(_ctx(
        body=body, transactions_data=txns,
        now_et=datetime(2026, 4, 25, 15, 0, tzinfo=timezone.utc),
    ))
    assert p.get("consecutive_losing_closes_24h") == 2


def test_consecutive_losing_closes_24h_unevaluatable_without_data():
    p = FieldProvider(_ctx(body=_equity_body()))
    with pytest.raises(UnevaluatableField, match="transactions_data"):
        p.get("consecutive_losing_closes_24h")


# ---- position state ------------------------------------------------------


def test_existing_position_qty_for_equity():
    pos = [_equity_position("NVDA", qty=150)]
    p = FieldProvider(_ctx(
        body=_equity_body(symbol="NVDA"),
        account_data=_account(positions=pos),
    ))
    assert p.get("existing_position_qty") == 150


def test_existing_position_count_per_ticker():
    pos = [
        _option_position("NVDA  260117C00250000", long_qty=1),
        _option_position("NVDA  260117P00200000", long_qty=2),
        _option_position("AMZN  260117C00300000", long_qty=1),
    ]
    p = FieldProvider(_ctx(
        body=_option_short_call_body(),
        account_data=_account(positions=pos),
    ))
    assert p.get("existing_position_count_per_ticker") == 2


def test_concentration_pct_sums_underlying_positions():
    pos = [
        _equity_position("NVDA", qty=200, market_value=50000.0),
        _option_position("NVDA  260117C00250000", long_qty=1, market_value=300.0),
    ]
    p = FieldProvider(_ctx(
        body=_option_short_call_body(),
        account_data=_account(positions=pos, net_liq=100000.0),
    ))
    # 50,300 / 100,000 = 50.30%
    assert abs(p.get("concentration_pct") - 50.3) < 0.01


def test_covered_by_equity_true_when_enough_shares():
    pos = [_equity_position("NVDA", qty=100)]   # exactly 1 contract worth
    p = FieldProvider(_ctx(
        body=_option_short_call_body(qty=1),    # 1 short call
        account_data=_account(positions=pos),
    ))
    assert p.get("covered_by_equity") is True


def test_covered_by_equity_false_when_too_few_shares():
    pos = [_equity_position("NVDA", qty=50)]
    p = FieldProvider(_ctx(
        body=_option_short_call_body(qty=1),
        account_data=_account(positions=pos),
    ))
    assert p.get("covered_by_equity") is False


def test_cash_secured_for_short_put_true_when_cash_sufficient():
    body = {
        "orderType": "LIMIT", "price": "1.00", "quantity": 1,
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN", "quantity": 1,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "NVDA  260117P00200000",  # strike 200
            },
        }],
    }
    p = FieldProvider(_ctx(
        body=body, account_data=_account(positions=[], cash=25000.0),
    ))
    # Required: 200 * 100 * 1 = 20,000. Cash 25,000 → True.
    assert p.get("cash_secured_for_short_put") is True


def test_cash_secured_for_short_put_false_when_cash_short():
    body = {
        "orderType": "LIMIT", "price": "1.00", "quantity": 1,
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN", "quantity": 1,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "NVDA  260117P00200000",
            },
        }],
    }
    p = FieldProvider(_ctx(
        body=body, account_data=_account(positions=[], cash=15000.0),
    ))
    assert p.get("cash_secured_for_short_put") is False


def test_covered_by_pmcc_true_when_long_call_lower_strike_longer_expiry():
    # Short the 250C Jan 17 26; we own a long 200C July 17 27 — covers it.
    pos = [_option_position(
        "NVDA  270717C00200000", long_qty=1, market_value=8000.0,
    )]
    p = FieldProvider(_ctx(
        body=_option_short_call_body(strike="00250000", expiry="260117"),
        account_data=_account(positions=pos),
    ))
    assert p.get("covered_by_pmcc") is True


def test_covered_by_pmcc_false_when_long_call_higher_strike():
    # Long 280C — strike higher than the short 250C — does NOT cover.
    pos = [_option_position(
        "NVDA  270717C00280000", long_qty=1, market_value=2000.0,
    )]
    p = FieldProvider(_ctx(
        body=_option_short_call_body(strike="00250000", expiry="260117"),
        account_data=_account(positions=pos),
    ))
    assert p.get("covered_by_pmcc") is False


def test_position_field_unevaluatable_without_account_data():
    p = FieldProvider(_ctx(body=_option_short_call_body()))
    with pytest.raises(UnevaluatableField, match="account_data"):
        p.get("existing_position_qty")


# ---- source registry coverage --------------------------------------------


def test_phase_2c_fields_have_source_mapping():
    from schwab_cli.order_policy.fields import PHASE_2C_FIELDS
    from schwab_cli.order_policy.sources import FIELD_SOURCE
    for f in PHASE_2C_FIELDS:
        assert f in FIELD_SOURCE, f"missing source mapping for {f}"


def test_required_sources_picks_counters_for_daily_order_count():
    from schwab_cli.order_policy import parse_profile
    from schwab_cli.order_policy.sources import (
        referenced_fields, required_sources,
    )
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "limit_daily", "match": "*",
            "conditions": [{"daily_order_count": {"lte": 8}}],
            "effect": "allow",
        }],
    }, name="t")
    sources = required_sources(referenced_fields(prof))
    assert sources == {"counters"}


def test_required_sources_picks_transactions_for_losing_closes():
    from schwab_cli.order_policy import parse_profile
    from schwab_cli.order_policy.sources import (
        referenced_fields, required_sources,
    )
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "loss_cooldown", "match": "*",
            "conditions": [{"consecutive_losing_closes_24h": {"lte": 2}}],
            "effect": "allow",
        }],
    }, name="t")
    sources = required_sources(referenced_fields(prof))
    assert sources == {"transactions"}
