"""Tests for the order-panel POP (probability of profit) enrichment.

The label was previously a static placeholder ("deferred to Phase 2 —
needs live IV"). Now ``compute_analytics`` accepts an optional chain
payload and ``body`` so it can build :class:`PricedLeg` objects and
delegate to the existing ``analytics.strategy.pop`` function. The
renderer prints "Prob of Profit: NN.N%" (or "(unavailable …)" when no
chain was fetched).

These tests pin both the math wiring and the rendered label.
"""
from __future__ import annotations

from schwab_cli.output.orders import (
    OrderAnalytics,
    PreviewSummary,
    compute_analytics,
    render_confirmation,
)


# ---- chain envelope captured shape ---------------------------------------


def _ko_chain_envelope_for_short_put_73() -> dict:
    """Minimal chain envelope (output of ``shape_envelope``) carrying
    just the contracts ``_compute_pop`` needs — KO 29 May 2026 73 PUT
    plus an ATM-ish call so ``_anchor_iv`` has something to work with.
    """
    return {
        "symbol": "KO",
        "expiry": "2026-05-29",
        "dte": 32,
        "underlying": {"last": 76.18, "netChange": -0.45, "pctChange": -0.6},
        "contracts": [
            # The leg's matching contract.
            {
                "side": "P", "strike": 73.0, "bid": 0.78, "ask": 0.82,
                "mark": 0.80, "iv": 0.20,
                "delta": -0.20, "gamma": 0.05, "theta": -0.04, "vega": 0.06,
            },
            # An ATM call for IV anchoring.
            {
                "side": "C", "strike": 76.0, "bid": 1.20, "ask": 1.30,
                "mark": 1.25, "iv": 0.18,
                "delta": 0.50, "gamma": 0.06, "theta": -0.05, "vega": 0.08,
            },
        ],
    }


def _short_put_body() -> dict:
    return {
        "orderType": "LIMIT",
        "session": "NORMAL", "duration": "DAY",
        "complexOrderStrategyType": "NONE", "quantity": 1,
        "orderStrategyType": "SINGLE",
        "price": "0.80",
        "orderLegCollection": [
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": "KO    260529P00073000",  # 21-char OSI
                },
            },
        ],
    }


# ---- compute_analytics + POP wiring -------------------------------------


def test_short_put_pop_is_computed_when_chain_supplied():
    """Short OTM put far from spot ⇒ POP should be high (chance of
    expiring worthless). With KO at 76.18 and strike 73 (4% OTM), POP
    near 80% is the expected ballpark."""
    analytics = compute_analytics(
        strategy=None, side="SELL", option_type="PUT",
        strikes=(73.0,), quantity=1, price=0.80,
        body=_short_put_body(),
        chain_data=_ko_chain_envelope_for_short_put_73(),
    )
    assert analytics is not None
    assert analytics.pop is not None
    assert 0.0 <= analytics.pop <= 1.0
    # OTM short put with reasonable IV should have POP > 60%.
    assert analytics.pop > 0.6


def test_pop_is_none_when_no_chain_supplied():
    """Equity orders + dry-run in CI without chain access keep the
    deterministic payoff math but skip POP."""
    analytics = compute_analytics(
        strategy=None, side="SELL", option_type="PUT",
        strikes=(73.0,), quantity=1, price=0.80,
        body=_short_put_body(),
        chain_data=None,
    )
    assert analytics is not None
    assert analytics.pop is None


def test_pop_falls_back_to_none_when_chain_lacks_underlying_spot():
    """A malformed chain (no underlying.last) must not crash POP — it
    falls back to ``None`` and the renderer says (unavailable)."""
    bad_chain = _ko_chain_envelope_for_short_put_73()
    bad_chain["underlying"]["last"] = None
    analytics = compute_analytics(
        strategy=None, side="SELL", option_type="PUT",
        strikes=(73.0,), quantity=1, price=0.80,
        body=_short_put_body(), chain_data=bad_chain,
    )
    assert analytics is not None
    assert analytics.pop is None


# ---- render_confirmation: label + value -----------------------------------


_BASE_BODY = _short_put_body()


def _render(analytics: OrderAnalytics | None) -> str:
    return render_confirmation(
        body=_BASE_BODY,
        account_tail="0756",
        strategy_label="SELL 1 KO PUT",
        is_naked_short=True,
        analytics=analytics,
        preview=PreviewSummary(None, None, None, None, (), ()),
        preview_unavailable=True,
    )


def test_panel_shows_prob_of_profit_with_value_when_pop_present():
    a = OrderAnalytics(
        max_profit=80.0, max_loss=-7220.0, breakevens=(72.20,),
        order_cost=-80.0, pop=0.755,
    )
    out = _render(a)
    assert "Prob of Profit" in out
    assert "75.5%" in out
    # The deprecated placeholder must not slip back in.
    assert "deferred to Phase 2" not in out


def test_panel_shows_unavailable_when_pop_missing():
    a = OrderAnalytics(
        max_profit=80.0, max_loss=-7220.0, breakevens=(72.20,),
        order_cost=-80.0, pop=None,
    )
    out = _render(a)
    assert "Prob of Profit" in out
    assert "unavailable" in out
    assert "deferred to Phase 2" not in out
