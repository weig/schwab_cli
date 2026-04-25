"""Source-dependency analysis for field providers.

Each field maps to one of the data sources Phase 2b can populate:

* ``chain``   — option chain or equity quote (for spot/bid/ask/greeks/iv)
* ``account`` — ``getAccount`` payload (balances + positions)
* ``preview`` — ``previewOrder`` payload (BP impact, commission)
* ``calendar`` — NYSE market calendar (temporal fields)
* ``dividends`` — dividends payload (`days_to_ex_div`)
* ``none``    — derivable from the body alone

The CLI uses :func:`required_sources` to decide which Schwab calls
to make before evaluating a profile. ``preview`` is always present
(we already preview every order in Phase 1) so the only question
is whether to call ``getAccount`` / ``getChain`` / dividends.
"""

from __future__ import annotations

from typing import Iterable, Literal

from schwab_cli.order_policy.schema import (
    AndCondition,
    AnyOfMatch,
    AllOfMatch,
    Condition,
    FieldMatch,
    NotCondition,
    OrCondition,
    Predicate,
    Profile,
    WildcardMatch,
)

Source = Literal[
    "chain", "account", "preview", "calendar", "dividends",
    "counters", "transactions", "none",
]


# Single source of truth — every field implemented by Phase 2a/2b
# field providers maps to exactly one source.
FIELD_SOURCE: dict[str, Source] = {
    # --- 2a intrinsic + categorical (no I/O) ---
    "account": "none",
    "underlying": "none",
    "asset_type": "none",
    "option_side": "none",
    "instruction": "none",
    "order_type": "none",
    "duration": "none",
    "session": "none",
    "complex_strategy_type": "none",
    "order_source": "none",
    "quantity": "none",
    "strike": "none",
    "expiry": "none",
    "dte": "none",
    "price": "none",
    "order_value": "none",

    # --- 2b market data (chain + greeks) ---
    "spot": "chain",
    "bid": "chain",
    "ask": "chain",
    "mid": "chain",
    "mark": "chain",
    "delta": "chain",
    "gamma": "chain",
    "theta": "chain",
    "vega": "chain",
    "rho": "chain",
    "iv": "chain",
    "intrinsic": "chain",
    "extrinsic": "chain",

    # --- 2b strike-relative (derives from spot+strike) ---
    "strike_pct_of_spot": "chain",
    "strike_pct_above_spot": "chain",
    "strike_pct_below_spot": "chain",
    "moneyness": "chain",

    # --- 2b pricing-relative ---
    "price_pct_of_bid": "chain",
    "price_pct_of_ask": "chain",
    "price_pct_of_mid": "chain",
    "price_pct_of_mark": "chain",

    # --- 2b account state ---
    "net_liq": "account",
    "cash": "account",
    "bp_total": "account",
    "bp_used": "account",
    "bp_available": "account",
    "bp_used_pct": "account",
    "maint_req": "account",
    "maint_cushion": "account",
    "maint_cushion_pct": "account",

    # --- 2b BP impact (from preview response) ---
    "bp_required": "preview",
    "bp_required_pct": "preview",
    "bp_after_pct": "preview",
    "order_value_pct_of_netliq": "account",   # needs net_liq

    # --- 2b temporal ---
    "market_session": "calendar",
    "minutes_since_open": "calendar",
    "minutes_to_close": "calendar",
    "is_market_holiday": "calendar",

    # --- 2b dividends ---
    "days_to_ex_div": "dividends",

    # --- 2c counters ---
    "daily_order_count": "counters",
    "daily_order_count_per_ticker": "counters",
    "minutely_order_count": "counters",
    "replace_count": "counters",

    # --- 2c position state (rides on the same getAccount payload as
    # account state) ---
    "existing_position_qty": "account",
    "existing_position_count_per_ticker": "account",
    "concentration_pct": "account",
    "covered_by_equity": "account",
    "cash_secured_for_short_put": "account",
    "covered_by_pmcc": "account",

    # --- 2c transactions ---
    "consecutive_losing_closes_24h": "transactions",
}


def referenced_fields(profile: Profile) -> set[str]:
    """Walk every enabled policy and return the set of field names
    its match clauses + conditions actually reference.

    This is the minimal set the field provider may be asked for; the
    CLI uses it to pre-fetch only the data sources Phase 2b actually
    needs."""
    out: set[str] = set()
    for p in profile.policies:
        if not p.enabled:
            continue
        _walk_match(p.match, out)
        for c in p.conditions:
            _walk_condition(c, out)
    return out


def required_sources(fields: Iterable[str]) -> set[Source]:
    """Map a set of field names to the Source set the field provider
    needs. Unknown fields (which the provider raises
    :class:`UnevaluatableField` on) are silently skipped — they
    don't drive any fetch."""
    return {
        FIELD_SOURCE[f]
        for f in fields
        if f in FIELD_SOURCE and FIELD_SOURCE[f] != "none"
    }


def _walk_match(m, out: set[str]) -> None:
    if isinstance(m, WildcardMatch):
        return
    if isinstance(m, FieldMatch):
        out.update(m.fields.keys())
        out.update(m.negated_fields.keys())
        return
    if isinstance(m, (AnyOfMatch, AllOfMatch)):
        for c in m.clauses:
            _walk_match(c, out)
        return


def _walk_condition(c: Condition, out: set[str]) -> None:
    if isinstance(c, Predicate):
        out.add(c.field_name)
        return
    if isinstance(c, (AndCondition, OrCondition, NotCondition)):
        for child in c.children:
            _walk_condition(child, out)
        return
