"""ResolveNotionalQuantityRule: dollar-denominated equity → share qty.

Builds a minimal OrderContext and drives only the rule, with audit and
the on-demand quote fetch stubbed. The rule rewrites the body + leg
quantity (Schwab quantity is a double, so fractional passes through).
"""
from __future__ import annotations

import pytest

from schwab_cli.commands.order import (
    EXIT_USAGE, _NormalizedOrder, _build_body, _spec_from_ticket,
)
from schwab_cli.order_pipeline.context import OrderContext
from schwab_cli.order_pipeline.rules import ResolveNotionalQuantityRule
from schwab_cli.order_pipeline.runner import PipelineExit, run_pipeline
from schwab_cli.order_ticket import parse_ticket


class _Acct:
    account_number = "123456789"


def _ctx(spec, *, quote=None):
    return OrderContext(
        spec=spec, body=_build_body(spec), account=_Acct(), client=object(),
        sub="place", dry_run=False, yes=True, overriding=False,
        profile_name="default", override_reason=None, as_json=True,
        limits=None, underlying_quote=quote,
    )


def _run(ctx, monkeypatch, *, fetch_quote=None):
    monkeypatch.setattr("schwab_cli.commands.order._audit", lambda *a, **k: None)
    if fetch_quote is not None:
        monkeypatch.setattr(
            "schwab_cli.commands.order._fetch_underlying_quote_safe",
            lambda client, body: fetch_quote,
        )
    run_pipeline([ResolveNotionalQuantityRule()], ctx)


def test_limit_notional_uses_limit_price(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $10.00 of QQQ @735.83 LMT"))
    ctx = _ctx(spec)
    _run(ctx, monkeypatch)
    # 10 / 735.83 = 0.013590..., rounded to 4 dp
    assert ctx.body["quantity"] == 0.0136
    assert ctx.body["orderLegCollection"][0]["quantity"] == 0.0136
    assert ctx.spec.quantity == 0.0136


def test_market_notional_uses_live_quote(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $500.00 of QQQ MKT"))
    ctx = _ctx(spec, quote={"symbol": "QQQ", "last": 735.0})
    _run(ctx, monkeypatch)
    # 500 / 735 = 0.6803, 4dp
    assert ctx.body["quantity"] == 0.6803
    assert ctx.body["orderLegCollection"][0]["quantity"] == 0.6803


def test_market_notional_fetches_quote_when_missing(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $500.00 of QQQ MKT"))
    ctx = _ctx(spec, quote=None)
    _run(ctx, monkeypatch, fetch_quote={"symbol": "QQQ", "last": 250.0})
    assert ctx.body["quantity"] == 2.0


def test_whole_share_result_stays_int(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $1000.00 of FOO @250.00 LMT"))
    ctx = _ctx(spec)
    _run(ctx, monkeypatch)
    assert ctx.body["quantity"] == 4
    assert isinstance(ctx.body["quantity"], int)


def test_market_falls_back_to_ask_for_buy(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $1000.00 of QQQ MKT"))
    ctx = _ctx(spec, quote={"symbol": "QQQ", "last": None, "ask": 500.0, "bid": 490.0})
    _run(ctx, monkeypatch)
    assert ctx.body["quantity"] == 2.0   # 1000 / 500 (ask), not bid


def test_no_price_halts_with_usage_error(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY $500.00 of QQQ MKT"))
    ctx = _ctx(spec, quote={"symbol": "QQQ", "last": None})  # no usable price
    monkeypatch.setattr("schwab_cli.commands.order._audit", lambda *a, **k: None)
    monkeypatch.setattr(
        "schwab_cli.commands.order._fetch_underlying_quote_safe",
        lambda client, body: {"symbol": "QQQ", "last": None},
    )
    with pytest.raises(PipelineExit) as e:
        run_pipeline([ResolveNotionalQuantityRule()], ctx)
    assert e.value.exit_code == EXIT_USAGE


def test_share_count_order_is_noop(monkeypatch):
    spec = _spec_from_ticket(parse_ticket("BUY +100 NVDA @150.00 LMT"))
    ctx = _ctx(spec)
    _run(ctx, monkeypatch)
    assert ctx.body["quantity"] == 100   # unchanged


def test_place_rule_blocks_zero_quantity(monkeypatch):
    """Defense in depth: PlaceOrderRule refuses a non-positive quantity
    even if notional resolution were somehow skipped."""
    from schwab_cli.order_pipeline.rules import PlaceOrderRule

    spec = _spec_from_ticket(parse_ticket("BUY $10.00 of QQQ @735.83 LMT"))
    ctx = _ctx(spec)  # body quantity is the 0 placeholder (rule NOT run)
    monkeypatch.setattr("schwab_cli.commands.order._audit", lambda *a, **k: None)
    placed = []
    monkeypatch.setattr(
        "schwab_cli.commands.order._safe_place",
        lambda *a, **k: placed.append(1) or ("OID", None),
    )
    with pytest.raises(PipelineExit) as e:
        run_pipeline([PlaceOrderRule()], ctx)
    assert e.value.exit_code == EXIT_USAGE
    assert placed == []   # never reached Schwab
