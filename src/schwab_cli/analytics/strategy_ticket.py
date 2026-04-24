"""Schwab order-ticket renderer.

Produces a copy-paste-ready string matching Schwab's order-entry UI
conventions:

* Named shape::

    SELL -1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 260/255 CALL @0.85 LMT
    BUY +1 IRON CONDOR NVDA 100 (Weeklys) 1 MAY 26 210/207.5/197.5/192.5 CALL/PUT @1.60 LMT

* CUSTOM fallback (irregular ratios, non-standard shapes, multi-expiry
  not otherwise named)::

    BUY +1 2/1 CUSTOM AMZN 100 (Weeklys) 1 MAY 26/1 MAY 26 260/255 CALL/CALL @0.60 LMT

Conventions embedded here (observed from Schwab UI):

* Strikes list **descending**. For multi-side shapes, calls come before
  puts in that descending order.
* Side keyword compression:
  - Single side (VERTICAL, BUTTERFLY) → one keyword (``CALL`` or ``PUT``).
  - Both sides with natural pairing (STRADDLE, STRANGLE, IRON CONDOR,
    IRON BUTTERFLY) → compressed ``CALL/PUT``.
  - CUSTOM → one keyword per leg (``CALL/CALL`` or ``CALL/PUT/PUT`` …).
* Date format: ``D MON YY`` (``1 MAY 26``, ``15 MAY 26``).
* ``(Weeklys)`` tag appended only for non-standard-monthly expiries (not
  the third Friday of the month).
* Price after ``@`` is always positive absolute; SIDE (``BUY``/``SELL``)
  carries the credit/debit sign.
"""

from __future__ import annotations

from datetime import date

from schwab_cli.analytics.strategy import PricedLeg
from schwab_cli.analytics.strategy_classify import Classification


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def render_ticket(
    legs: list[PricedLeg],
    cls: Classification,
    *,
    symbol: str,
) -> str:
    """Return the Schwab-format order-ticket string for ``legs``.

    Uses :attr:`Classification.ticket_name` to pick the rendering path:
    empty → single-leg, ``CUSTOM`` → CUSTOM fallback, anything else →
    named-shape form.
    """
    if not legs:
        raise ValueError("render_ticket() requires at least one leg")

    net_premium_per_share = _net_premium(legs)
    side_word = "BUY" if net_premium_per_share < 0 else "SELL"
    # ±N is the spread quantity. For now we always emit ±1 spreads — the
    # parser doesn't accept a multi-spread quantity, and leg qtys
    # represent the ratios relative to one spread.
    n_tok = "+1" if side_word == "BUY" else "-1"
    abs_premium = abs(net_premium_per_share)
    price_tok = f"@{abs_premium:.2f} LMT"

    name = cls.ticket_name
    sym = symbol.upper()

    # 1-leg ----------------------------------------------------------
    if name == "" and len(legs) == 1:
        leg = legs[0]
        date_tok = _fmt_date(leg.expiry)
        weeklys = " (Weeklys)" if not _is_standard_monthly(leg.expiry) else ""
        return (
            f"{side_word} {n_tok} {sym} 100{weeklys} "
            f"{date_tok} {_fmt_strike(leg.strike)} "
            f"{_fmt_side(leg.side)} {price_tok}"
        )

    # CUSTOM fallback -----------------------------------------------
    if name == "CUSTOM":
        return _render_custom(legs, sym, side_word, n_tok, price_tok)

    # Named multi-leg shapes ---------------------------------------
    expiries = {leg.expiry for leg in legs}
    if len(expiries) == 1:
        date_tok = _fmt_date(next(iter(expiries)))
        weeklys = " (Weeklys)" if not _is_standard_monthly(next(iter(expiries))) else ""
    else:
        # Multi-expiry named (e.g. CALENDAR, DIAGONAL): slash-join the
        # expiries in leg order (sorted by expiry asc for stability).
        sorted_exps = sorted(expiries)
        date_tok = "/".join(_fmt_date(e) for e in sorted_exps)
        # Weeklys tag applies to *any* non-monthly expiry in the set.
        weeklys = (
            " (Weeklys)"
            if any(not _is_standard_monthly(e) for e in expiries)
            else ""
        )

    strike_tok, side_tok = _compressed_strikes_sides(legs, name)

    return (
        f"{side_word} {n_tok} {name} {sym} 100{weeklys} "
        f"{date_tok} {strike_tok} {side_tok} {price_tok}"
    )


# ---- helpers -----------------------------------------------------------


def _net_premium(legs: list[PricedLeg]) -> float:
    """Net premium per share. Positive = credit, negative = debit.

    Each long leg costs ``qty × premium`` (we paid it). Each short leg
    earns ``|qty| × premium``. Signed qty captures both::

        net = -Σ qty × premium

    so for a long call (qty=+1, premium=2.00), net = -2.00 (debit).
    """
    return -sum(leg.qty * leg.premium for leg in legs)


def _fmt_date(d: date) -> str:
    """Schwab-style ``D MON YY`` (no leading zero on day)."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year % 100:02d}"


def _fmt_strike(strike: float) -> str:
    """Strip trailing zeros: ``255`` not ``255.0``, ``192.5`` kept."""
    if strike == int(strike):
        return str(int(strike))
    # Up to 4 decimals; strip trailing zeros.
    s = f"{strike:.4f}".rstrip("0").rstrip(".")
    return s


def _fmt_side(side: str) -> str:
    return "CALL" if side == "C" else "PUT"


def _is_standard_monthly(d: date) -> bool:
    """``True`` when ``d`` is the third Friday of its month."""
    if d.weekday() != 4:  # Friday = 4 (Mon=0)
        return False
    # Third Friday's day-of-month falls in 15..21.
    return 15 <= d.day <= 21


def _compressed_strikes_sides(
    legs: list[PricedLeg], name: str
) -> tuple[str, str]:
    """Render the ``strikes / sides`` portion for named shapes.

    Conventions:

    * Strikes listed descending. For mixed-side shapes, calls first
      (also descending), then puts (descending).
    * Side keyword is ``CALL`` or ``PUT`` when legs all share a side;
      ``CALL/PUT`` when both sides are present (Schwab compresses the
      list even when there are 2 calls and 2 puts).
    """
    calls = sorted([L for L in legs if L.side == "C"], key=lambda L: -L.strike)
    puts = sorted([L for L in legs if L.side == "P"], key=lambda L: -L.strike)

    # For STRADDLE we want a single strike token (legs share it).
    if name == "STRADDLE":
        k = legs[0].strike
        return _fmt_strike(k), "CALL/PUT"

    if calls and puts:
        # Mixed sides — strikes are ordered all-calls-then-all-puts
        # (both groups descending). Side token compresses to CALL/PUT
        # for STRANGLE, IRON CONDOR, IRON BUTTERFLY.
        # IRON BUTTERFLY has a shared body strike — deduplicate while
        # preserving descending order for the final token.
        strike_seq = [L.strike for L in calls] + [L.strike for L in puts]
        if name == "IRON BUTTERFLY":
            # Collapse duplicate body strike to a single entry.
            seen: set[float] = set()
            uniq: list[float] = []
            for k in strike_seq:
                if k not in seen:
                    seen.add(k)
                    uniq.append(k)
            strike_seq = uniq
        return "/".join(_fmt_strike(k) for k in strike_seq), "CALL/PUT"

    # Single side (VERTICAL, BUTTERFLY).
    only = calls or puts
    strike_seq = [L.strike for L in only]
    side_word = _fmt_side(only[0].side)
    return "/".join(_fmt_strike(k) for k in strike_seq), side_word


def _render_custom(
    legs: list[PricedLeg],
    sym: str,
    side_word: str,
    n_tok: str,
    price_tok: str,
) -> str:
    """CUSTOM fallback: per-leg ratios, dates, strikes, sides.

    Leg ordering for the ticket: by side (calls before puts) then
    descending strike. Within equal strike (IRON BUTTERFLY body),
    calls before puts. Multi-expiry legs keep their own dates in the
    date list — Schwab requires one date per leg for CUSTOM even when
    all legs share the expiry.
    """
    ordered = sorted(
        legs,
        # calls (0) before puts (1), then descending strike.
        key=lambda L: (0 if L.side == "C" else 1, -L.strike),
    )

    # Absolute quantities for the ratio list — sign is carried by the
    # top-level SIDE keyword and individual-leg directions are implicit
    # in the order of the ratio/date/strike/side lists.
    ratios_tok = "/".join(str(abs(L.qty)) for L in ordered)
    dates_tok = "/".join(_fmt_date(L.expiry) for L in ordered)
    strikes_tok = "/".join(_fmt_strike(L.strike) for L in ordered)
    sides_tok = "/".join(_fmt_side(L.side) for L in ordered)

    # Weeklys tag: if any leg's expiry is non-monthly, mark it.
    weeklys = (
        " (Weeklys)"
        if any(not _is_standard_monthly(L.expiry) for L in legs)
        else ""
    )

    return (
        f"{side_word} {n_tok} {ratios_tok} CUSTOM {sym} 100{weeklys} "
        f"{dates_tok} {strikes_tok} {sides_tok} {price_tok}"
    )
