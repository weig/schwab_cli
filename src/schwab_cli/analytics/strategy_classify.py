"""Classify a list of :class:`Leg` into a named option strategy.

Pattern-matching only — no heuristics, no fuzzy names. If the legs
don't match one of the enumerated MVP shapes, we fall back to a generic
``"Custom {N}-leg"`` label with ``ticket_name="CUSTOM"``. That keeps
the name space finite and the ticket renderer honest.

The classifier also decides:

* ``supported`` — ``False`` for multi-expiry inputs (Phase 1 limitation)
  with a ``reason`` string. Single-expiry shapes are always supported
  even when we can't name them (CUSTOM still gets full analytics).
* ``naked`` — any short leg without a covering long of the same side.
  Naked short call → unlimited loss; naked short put → bounded but
  still flagged so the renderer can warn.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as _replace
from functools import reduce
from math import gcd

from schwab_cli.analytics.strategy_legs import Leg


@dataclass(frozen=True)
class Classification:
    strategy: str              # Human label, e.g. "Bull Call Spread"
    ticket_name: str           # Schwab keyword, e.g. "VERTICAL", "" for 1-leg, "CUSTOM"
    supported: bool            # False only when reason is set
    reason: str | None = None  # Why unsupported (e.g. "multi-expiry")
    naked: bool = False        # Any uncovered short leg


def classify(legs: list[Leg]) -> Classification:
    """Return the :class:`Classification` for a leg list.

    Empty input is a programming error (caller should have validated) —
    raises :class:`ValueError`. Otherwise always returns a
    Classification; unknown shapes come back as CUSTOM.

    Quantities are gcd-normalized before pattern-matching so that a
    multi-spread order (``-2 CONDOR`` with leg qtys 2/2/2/2) classifies
    the same as a single spread (1/1/1/1). The spread count itself is
    not surfaced — callers that need it (the ticket renderer) compute
    it from the original legs.
    """
    if not legs:
        raise ValueError("classify() requires at least one leg")

    expiries = {leg.expiry for leg in legs}
    multi_expiry = len(expiries) > 1

    # Naked / multi-expiry detection use the original legs — naked
    # status is invariant under scaling, and expiry diversity isn't
    # affected by qty.
    naked = _is_naked(legs)
    legs = _reduce_qtys(legs)

    if multi_expiry:
        # Name the shape where we can (CALENDAR / DIAGONAL) so the
        # ticket renderer produces a usable string even though analytics
        # are deferred to Phase 2.
        ticket = _multi_expiry_ticket(legs)
        strategy = {
            "CALENDAR": "Calendar Spread",
            "DIAGONAL": "Diagonal Spread",
        }.get(ticket, f"Custom {len(legs)}-leg (multi-expiry)")
        return Classification(
            strategy=strategy,
            ticket_name=ticket,
            supported=False,
            reason="multi-expiry",
            naked=naked,
        )

    # Single-expiry — dispatch by leg count.
    n = len(legs)
    if n == 1:
        return _classify_single(legs[0], naked=naked)
    if n == 2:
        return _classify_two(legs, naked=naked)
    if n == 3:
        return _classify_three(legs, naked=naked)
    if n == 4:
        return _classify_four(legs, naked=naked)

    return Classification(
        strategy=f"Custom {n}-leg",
        ticket_name="CUSTOM",
        supported=True,
        naked=naked,
    )


# ---- 1-leg -------------------------------------------------------------


def _classify_single(leg: Leg, *, naked: bool) -> Classification:
    direction = "Long" if leg.is_long else "Short"
    side = "Call" if leg.side == "C" else "Put"
    return Classification(
        strategy=f"{direction} {side}",
        ticket_name="",
        supported=True,
        naked=naked,
    )


# ---- 2-leg -------------------------------------------------------------


def _classify_two(legs: list[Leg], *, naked: bool) -> Classification:
    a, b = sorted(legs, key=lambda L: (L.side, L.strike))
    # Same side, different strikes, opposite signs, 1:1 qty → vertical.
    if (
        a.side == b.side
        and a.strike != b.strike
        and abs(a.qty) == abs(b.qty) == 1
        and a.qty * b.qty < 0
    ):
        lo, hi = (a, b) if a.strike < b.strike else (b, a)
        if lo.side == "C":
            strat = "Bull Call Spread" if lo.is_long else "Bear Call Spread"
        else:
            strat = "Bull Put Spread" if lo.is_long else "Bear Put Spread"
        return Classification(
            strategy=strat,
            ticket_name="VERTICAL",
            supported=True,
            naked=naked,
        )

    # Different sides, same strike, same sign, 1:1 qty → straddle.
    if (
        a.side != b.side
        and a.strike == b.strike
        and abs(a.qty) == abs(b.qty) == 1
        and a.qty * b.qty > 0
    ):
        strat = "Long Straddle" if a.is_long else "Short Straddle"
        return Classification(
            strategy=strat,
            ticket_name="STRADDLE",
            supported=True,
            naked=naked,
        )

    # Different sides, different strikes, same sign, 1:1 qty → strangle.
    if (
        a.side != b.side
        and a.strike != b.strike
        and abs(a.qty) == abs(b.qty) == 1
        and a.qty * b.qty > 0
    ):
        strat = "Long Strangle" if a.is_long else "Short Strangle"
        return Classification(
            strategy=strat,
            ticket_name="STRANGLE",
            supported=True,
            naked=naked,
        )

    return Classification(
        strategy="Custom 2-leg",
        ticket_name="CUSTOM",
        supported=True,
        naked=naked,
    )


# ---- 3-leg (butterflies) -----------------------------------------------


def _classify_three(legs: list[Leg], *, naked: bool) -> Classification:
    # All same side, 3 distinct strikes, qty pattern 1:-2:1 or -1:2:-1
    # (sorted ascending by strike), equal wing widths → Butterfly.
    if len({leg.side for leg in legs}) == 1:
        by_strike = sorted(legs, key=lambda L: L.strike)
        k1, k2, k3 = (leg.strike for leg in by_strike)
        q1, q2, q3 = (leg.qty for leg in by_strike)
        side = by_strike[0].side
        long_fly_pattern = (q1, q2, q3) == (1, -2, 1)
        short_fly_pattern = (q1, q2, q3) == (-1, 2, -1)
        equal_wings = abs((k2 - k1) - (k3 - k2)) < 1e-6
        three_distinct = len({k1, k2, k3}) == 3

        if three_distinct and (long_fly_pattern or short_fly_pattern):
            direction = "Long" if long_fly_pattern else "Short"
            side_word = "Call" if side == "C" else "Put"
            if equal_wings:
                return Classification(
                    strategy=f"{direction} {side_word} Butterfly",
                    ticket_name="BUTTERFLY",
                    supported=True,
                    naked=naked,
                )
            # Same pattern but unequal wings → broken-wing fly.
            return Classification(
                strategy=f"Broken-Wing {side_word} Fly",
                ticket_name="CUSTOM",
                supported=True,
                naked=naked,
            )

    return Classification(
        strategy="Custom 3-leg",
        ticket_name="CUSTOM",
        supported=True,
        naked=naked,
    )


# ---- 4-leg (iron condor / iron butterfly) ------------------------------


def _classify_four(legs: list[Leg], *, naked: bool) -> Classification:
    puts = sorted([L for L in legs if L.side == "P"], key=lambda L: L.strike)
    calls = sorted([L for L in legs if L.side == "C"], key=lambda L: L.strike)

    # Same-side CONDOR (4 calls or 4 puts) — long has signs +/-/-/+
    # ascending, short flips. Equidistant strikes optional; mismatch
    # could be Schwab's "Broken Wing Condor" but the keyword is the
    # same on the order ticket.
    same_side = (
        calls if len(calls) == 4 else puts if len(puts) == 4 else None
    )
    if same_side is not None and all(abs(L.qty) == 1 for L in same_side):
        by_strike = sorted(same_side, key=lambda L: L.strike)
        qs = tuple(L.qty for L in by_strike)
        side_word = "Call" if same_side[0].side == "C" else "Put"
        if len({L.strike for L in by_strike}) == 4:
            if qs == (1, -1, -1, 1):
                return Classification(
                    strategy=f"Long {side_word} Condor",
                    ticket_name="CONDOR",
                    supported=True,
                    naked=naked,
                )
            if qs == (-1, 1, 1, -1):
                return Classification(
                    strategy=f"Short {side_word} Condor",
                    ticket_name="CONDOR",
                    supported=True,
                    naked=naked,
                )

    if len(puts) == 2 and len(calls) == 2:
        # Must have all four 1:1 qty magnitudes.
        if all(abs(L.qty) == 1 for L in legs):
            p_low, p_high = puts
            c_low, c_high = calls

            # Iron condor topology: put wing (low strike) is long, put
            # body (higher strike) is short; call body (low strike) is
            # short, call wing (high strike) is long. Strikes ordered:
            # p_low < p_high ≤ c_low < c_high.
            ic_credit_topology = (
                p_low.is_long and p_high.is_short
                and c_low.is_short and c_high.is_long
                and p_high.strike <= c_low.strike
            )
            # Reverse IC: all four signs flipped.
            ic_debit_topology = (
                p_low.is_short and p_high.is_long
                and c_low.is_long and c_high.is_short
                and p_high.strike <= c_low.strike
            )

            if ic_credit_topology or ic_debit_topology:
                # Iron butterfly special case: both body strikes equal.
                if p_high.strike == c_low.strike:
                    direction = "" if ic_credit_topology else "Reverse "
                    return Classification(
                        strategy=f"{direction}Iron Butterfly".strip(),
                        ticket_name="IRON BUTTERFLY",
                        supported=True,
                        naked=naked,
                    )
                direction = "" if ic_credit_topology else "Reverse "
                return Classification(
                    strategy=f"{direction}Iron Condor".strip(),
                    ticket_name="IRON CONDOR",
                    supported=True,
                    naked=naked,
                )

    return Classification(
        strategy="Custom 4-leg",
        ticket_name="CUSTOM",
        supported=True,
        naked=naked,
    )


# ---- multi-expiry naming (for ticket rendering only) -------------------


def _multi_expiry_ticket(legs: list[Leg]) -> str:
    """Return the Schwab ticket keyword for a multi-expiry position,
    falling back to ``"CUSTOM"`` when the shape isn't a textbook
    calendar or diagonal."""
    if len(legs) != 2:
        return "CUSTOM"
    a, b = legs
    # Same side, opposite signs, 1:1 qty.
    if (
        a.side == b.side
        and abs(a.qty) == abs(b.qty) == 1
        and a.qty * b.qty < 0
    ):
        if a.strike == b.strike:
            return "CALENDAR"
        return "DIAGONAL"
    return "CUSTOM"


# ---- qty normalization -------------------------------------------------


def _reduce_qtys(legs: list[Leg]) -> list[Leg]:
    """Return ``legs`` with each ``qty`` divided by the gcd of |qty|.

    Spread orders carry leg ratios already scaled by the spread count
    (``SELL -2 CONDOR`` → leg qtys 2/2/2/2). Pattern-matching uses unit
    ratios (1/1/1/1), so we strip the common factor up front.
    """
    if not legs:
        return list(legs)
    g = reduce(gcd, (abs(L.qty) for L in legs))
    if g <= 1:
        return list(legs)
    return [_replace(L, qty=L.qty // g) for L in legs]


# ---- naked detection ---------------------------------------------------


def _is_naked(legs: list[Leg]) -> bool:
    """True if total short contracts exceed total long contracts on
    either side.

    A long option of any strike caps a short option of the same side:
    at large |S-K|, both have slope ±1, so longs and shorts cancel
    1:1. Only the net count matters for unlimited-exposure detection.
    Per-strike coverage matters for max-loss magnitude but not for the
    naked flag — that's handled in the P/L computation.

    Expiries are not considered for this flag.
    """
    short_calls = sum(-leg.qty for leg in legs if leg.side == "C" and leg.is_short)
    long_calls = sum(leg.qty for leg in legs if leg.side == "C" and leg.is_long)
    short_puts = sum(-leg.qty for leg in legs if leg.side == "P" and leg.is_short)
    long_puts = sum(leg.qty for leg in legs if leg.side == "P" and leg.is_long)
    return short_calls > long_calls or short_puts > long_puts
