"""Phase 2b unit tests — market data, strike-relative, pricing-relative,
account state, BP impact, temporal, dividends.

Pure: no Schwab calls, no I/O. We hand the field provider hand-crafted
``chain_data`` / ``account_data`` / ``preview_data`` / ``quote_data`` /
``dividend_data`` payloads matching the shapes Schwab returns.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.order_policy.conditions import UnevaluatableField
from schwab_cli.order_policy.fields import FieldProvider, OrderContext
from schwab_cli.order_policy.sources import (
    FIELD_SOURCE,
    referenced_fields,
    required_sources,
)
from schwab_cli.order_policy import parse_profile


# ---- shared fixtures ------------------------------------------------------


_NVDA_C250 = "NVDA  260117C00250000"
_NVDA_C260 = "NVDA  260117C00260000"


def _option_body():
    return {
        "session": "NORMAL", "duration": "DAY",
        "orderType": "NET_DEBIT", "price": "2.35", "quantity": 1,
        "complexOrderStrategyType": "VERTICAL",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION", "symbol": _NVDA_C250}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION", "symbol": _NVDA_C260}},
        ],
    }


def _equity_body(symbol="AAPL", side="BUY", qty=10, price="150.00"):
    return {
        "session": "NORMAL", "duration": "DAY",
        "orderType": "LIMIT", "price": price, "quantity": qty,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": side, "quantity": qty,
            "instrument": {"assetType": "EQUITY", "symbol": symbol},
        }],
    }


def _chain_payload():
    return {
        "underlying": {"last": 245.0},
        "callExpDateMap": {
            "2026-01-17:0": {
                "250.0": [{
                    "symbol": _NVDA_C250, "strikePrice": 250.0,
                    "bid": 3.0, "ask": 3.4, "mark": 3.2,
                    "delta": 0.45, "gamma": 0.01,
                    "theta": -0.04, "vega": 0.18, "rho": 0.08,
                    "volatility": 28.5,
                    "intrinsicValue": 0.0, "timeValue": 3.2,
                }],
                "260.0": [{
                    "symbol": _NVDA_C260, "strikePrice": 260.0,
                    "bid": 1.0, "ask": 1.2, "mark": 1.1,
                    "delta": 0.20, "gamma": 0.008,
                    "theta": -0.03, "vega": 0.12, "rho": 0.04,
                    "volatility": 26.0,
                    "intrinsicValue": 0.0, "timeValue": 1.1,
                }],
            },
        },
    }


def _account_payload():
    return {
        "securitiesAccount": {
            "currentBalances": {
                "liquidationValue": 100000.0,
                "cashBalance": 30000.0,
                "buyingPower": 70000.0,
                "longMarketValue": 5000.0,
                "shortMarketValue": 0.0,
                "maintenanceRequirement": 20000.0,
            }
        },
    }


def _preview_payload():
    return {"orderValueImpact": {"buyingPowerEffect": -1500.0}}


def _ctx(*, body, **slots):
    return OrderContext(
        body=body,
        account_number="12345678",
        today=date(2026, 4, 25),
        now_et=datetime(2026, 4, 24, 11, 0, tzinfo=ZoneInfo("America/New_York")),
        **slots,
    )


# ---- market data — option ------------------------------------------------


def test_md_option_basic_fields_resolved_from_chain():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    assert p.get("spot") == 245.0
    assert p.get("bid") == 3.0
    assert p.get("ask") == 3.4
    assert p.get("mark") == 3.2
    assert p.get("delta") == 0.45
    assert p.get("gamma") == 0.01
    assert p.get("theta") == -0.04
    assert p.get("vega") == 0.18
    assert p.get("rho") == 0.08
    assert p.get("iv") == 28.5
    assert p.get("intrinsic") == 0.0
    assert p.get("extrinsic") == 3.2
    assert abs(p.get("mid") - 3.2) < 1e-9


def test_md_option_unevaluatable_when_no_chain():
    p = FieldProvider(_ctx(body=_option_body()))
    with pytest.raises(UnevaluatableField, match="chain_data"):
        p.get("delta")


def test_md_option_unevaluatable_when_contract_not_in_chain():
    chain = _chain_payload()
    # Drop the C250 contract.
    chain["callExpDateMap"]["2026-01-17:0"].pop("250.0")
    p = FieldProvider(_ctx(body=_option_body(), chain_data=chain))
    with pytest.raises(UnevaluatableField, match="not found in chain_data"):
        p.get("delta")


# ---- market data — equity ------------------------------------------------


def _quote_payload(symbol="AAPL", last=150.0, bid=149.95, ask=150.05, mark=150.0):
    return {symbol: {"quote": {
        "lastPrice": last, "bidPrice": bid,
        "askPrice": ask, "mark": mark,
    }}}


def test_md_equity_quote():
    p = FieldProvider(_ctx(
        body=_equity_body(),
        quote_data=_quote_payload(),
    ))
    assert p.get("spot") == 150.0
    assert p.get("bid") == 149.95
    assert p.get("ask") == 150.05
    assert p.get("mark") == 150.0
    assert abs(p.get("mid") - 150.0) < 1e-9


def test_md_equity_unevaluatable_for_greeks():
    p = FieldProvider(_ctx(
        body=_equity_body(),
        quote_data=_quote_payload(),
    ))
    for f in ("delta", "gamma", "theta", "vega", "rho", "iv",
              "intrinsic", "extrinsic"):
        with pytest.raises(UnevaluatableField):
            p.get(f)


# ---- strike-relative -----------------------------------------------------


def test_strike_pct_above_spot():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    # strike 250 vs spot 245 → +2.04%
    assert abs(p.get("strike_pct_above_spot") - 2.04) < 0.01


def test_strike_pct_below_spot():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    assert abs(p.get("strike_pct_below_spot") - (-2.04)) < 0.01


def test_strike_pct_of_spot():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    assert abs(p.get("strike_pct_of_spot") - (250.0 / 245.0 * 100.0)) < 1e-6


def test_moneyness_atm_otm_buckets():
    # Strike at 245 + spot 245 → ATM.
    chain = _chain_payload()
    chain["underlying"]["last"] = 250.0  # strike == spot for the BTO leg
    p = FieldProvider(_ctx(body=_option_body(), chain_data=chain))
    assert p.get("moneyness") == "atm"

    # Long call at 250, spot 245 → OTM (strike > spot for a CALL).
    chain["underlying"]["last"] = 245.0
    p2 = FieldProvider(_ctx(body=_option_body(), chain_data=chain))
    assert p2.get("moneyness") == "otm"


# ---- pricing-relative ----------------------------------------------------


def test_price_pct_of_mid_for_option():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    # order price 2.35 vs mid 3.2 → ~73.4%
    assert abs(p.get("price_pct_of_mid") - (2.35 / 3.2 * 100.0)) < 1e-6


def test_price_pct_of_bid_and_ask():
    p = FieldProvider(_ctx(body=_option_body(), chain_data=_chain_payload()))
    assert abs(p.get("price_pct_of_bid") - (2.35 / 3.0 * 100.0)) < 1e-6
    assert abs(p.get("price_pct_of_ask") - (2.35 / 3.4 * 100.0)) < 1e-6


# ---- account state -------------------------------------------------------


def test_account_state_basics():
    p = FieldProvider(_ctx(body=_option_body(),
                           chain_data=_chain_payload(),
                           account_data=_account_payload()))
    assert p.get("net_liq") == 100000.0
    assert p.get("cash") == 30000.0
    assert p.get("bp_total") == 70000.0
    assert p.get("bp_used") == 5000.0  # long_mv + |short_mv|
    assert p.get("bp_available") == 65000.0
    assert abs(p.get("bp_used_pct") - (5000.0 / 70000.0 * 100.0)) < 1e-6
    assert p.get("maint_req") == 20000.0
    assert p.get("maint_cushion") == 80000.0
    assert abs(p.get("maint_cushion_pct") - 80.0) < 1e-6


def test_account_state_unevaluatable_without_account_data():
    p = FieldProvider(_ctx(body=_option_body()))
    with pytest.raises(UnevaluatableField, match="account_data"):
        p.get("net_liq")


# ---- BP impact -----------------------------------------------------------


def test_bp_required_from_preview():
    p = FieldProvider(_ctx(body=_option_body(),
                           chain_data=_chain_payload(),
                           preview_data=_preview_payload()))
    assert p.get("bp_required") == 1500.0  # abs of buyingPowerEffect


def test_bp_required_pct_needs_both_preview_and_account():
    # Without account_data → unevaluatable.
    p_only_preview = FieldProvider(_ctx(
        body=_option_body(), preview_data=_preview_payload(),
    ))
    with pytest.raises(UnevaluatableField, match="account_data"):
        p_only_preview.get("bp_required_pct")
    # Both → derived.
    p = FieldProvider(_ctx(
        body=_option_body(),
        chain_data=_chain_payload(),
        account_data=_account_payload(),
        preview_data=_preview_payload(),
    ))
    assert abs(p.get("bp_required_pct") - (1500.0 / 70000.0 * 100.0)) < 1e-6


def test_bp_after_pct():
    p = FieldProvider(_ctx(
        body=_option_body(),
        chain_data=_chain_payload(),
        account_data=_account_payload(),
        preview_data=_preview_payload(),
    ))
    used_pct = 5000.0 / 70000.0 * 100.0
    req_pct = 1500.0 / 70000.0 * 100.0
    assert abs(p.get("bp_after_pct") - (used_pct + req_pct)) < 1e-6


def test_order_value_pct_of_netliq():
    p = FieldProvider(_ctx(
        body=_option_body(),
        chain_data=_chain_payload(),
        account_data=_account_payload(),
    ))
    # order_value = 2.35 * 1 * 100 = 235; netliq = 100000.
    assert abs(p.get("order_value_pct_of_netliq") - 0.235) < 1e-6


# ---- temporal ------------------------------------------------------------


def test_temporal_regular_session():
    p = FieldProvider(_ctx(body=_option_body()))
    # 11:00 ET on a non-holiday weekday — REGULAR.
    assert p.get("market_session") == "REGULAR"
    assert p.get("minutes_since_open") == 90      # 09:30 → 11:00
    assert 290 <= p.get("minutes_to_close") <= 300


def test_temporal_pre_session():
    p = FieldProvider(_ctx(
        body=_option_body(),
        # 7am ET on a weekday.
    ))
    p._ctx = OrderContext(
        body=_option_body(),
        account_number="12345678",
        today=date(2026, 4, 24),
        now_et=datetime(2026, 4, 24, 7, 0,
                        tzinfo=ZoneInfo("America/New_York")),
    )
    p._cats = type(p)._compute_2b.__globals__["categorical_view"](p._ctx)
    p._cache.clear()
    assert p.get("market_session") == "PRE"
    with pytest.raises(UnevaluatableField):
        p.get("minutes_since_open")


def test_temporal_holiday_closed():
    p = FieldProvider(_ctx(
        body=_option_body(),
    ))
    # Christmas 2026.
    p._ctx = OrderContext(
        body=_option_body(),
        account_number="12345678",
        today=date(2026, 12, 25),
        now_et=datetime(2026, 12, 25, 11, 0,
                        tzinfo=ZoneInfo("America/New_York")),
    )
    p._cats = type(p)._compute_2b.__globals__["categorical_view"](p._ctx)
    p._cache.clear()
    assert p.get("is_market_holiday") is True
    assert p.get("market_session") == "CLOSED"


# ---- dividends -----------------------------------------------------------


def test_days_to_ex_div():
    p = FieldProvider(_ctx(
        body=_equity_body(symbol="KO"),
        dividend_data={"KO": {"nextDividendDate": "2026-05-08"}},
    ))
    # today = 2026-04-25 → 13 days
    assert p.get("days_to_ex_div") == 13


def test_days_to_ex_div_unknown_symbol_raises():
    p = FieldProvider(_ctx(
        body=_equity_body(symbol="KO"),
        dividend_data={"PEP": {"nextDividendDate": "2026-05-15"}},
    ))
    with pytest.raises(UnevaluatableField):
        p.get("days_to_ex_div")


# ---- source registry / minimal-fetch analysis ---------------------------


def test_field_source_table_covers_all_2b_fields():
    from schwab_cli.order_policy.fields import PHASE_2B_FIELDS
    for f in PHASE_2B_FIELDS:
        assert f in FIELD_SOURCE, f"missing source mapping for {f}"


def test_referenced_fields_walks_match_and_conditions():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p1",
            "match": {"underlying": ["KO"], "instruction": ["SELL_TO_OPEN"]},
            "conditions": [
                {"delta": {"gte": -0.30}},
                {"and": [{"dte": {"gte": 60}}, {"bp_required_pct": {"lte": 5}}]},
            ],
            "effect": "allow",
        }],
    }, name="t")
    refs = referenced_fields(prof)
    assert {"underlying", "instruction", "delta", "dte", "bp_required_pct"} <= refs


def test_required_sources_minimal_set():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "needs_chain_only",
            "match": "*",
            "conditions": [{"delta": {"gte": -0.3}}],
            "effect": "allow",
        }],
    }, name="t")
    refs = referenced_fields(prof)
    sources = required_sources(refs)
    assert sources == {"chain"}


def test_required_sources_zero_for_intrinsic_only():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "intrinsic_only",
            "match": {"underlying": ["KO"]},
            "conditions": [{"quantity": {"lte": 100}}],
            "effect": "allow",
        }],
    }, name="t")
    sources = required_sources(referenced_fields(prof))
    assert sources == set()


def test_required_sources_disabled_policy_ignored():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "expensive_off",
            "enabled": False,
            "match": "*",
            "conditions": [{"delta": {"gte": -0.3}}],
            "effect": "allow",
        }],
    }, name="t")
    assert required_sources(referenced_fields(prof)) == set()
