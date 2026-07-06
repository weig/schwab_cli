"""Locate the screener's target put on a raw Schwab option chain.

Pure functions — no I/O. The target is the put whose chain-returned delta
is closest to -0.25, on the standard-monthly expiry nearest 30 DTE within a
[25, 35] window (falling back to any in-window expiry when no monthly is
present). We deliberately use the chain's own delta (not a self-computed
one) so the located contract is exactly what live execution would trade.

``flatten_chain`` in api/chains.py drops bid/ask, so this module parses the
raw ``putExpDateMap`` directly to keep the quote fields the screener needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

TARGET_DTE = 30
DTE_LO = 25
DTE_HI = 35
TARGET_PUT_DELTA = -0.25
# Reject a located put whose delta falls outside this band as bad data
# (a garbage delta field would otherwise select an absurd strike).
DELTA_BAND = (-0.35, -0.15)


@dataclass(frozen=True)
class TargetPut:
    expiry: str  # ISO "YYYY-MM-DD"
    dte: int
    strike: float
    delta: float
    bid: float
    ask: float
    mid: float
    open_interest: int
    volume: int
    spread_pct: float | None  # (ask-bid)/mid; None when mid <= 0


def underlying_last(raw: dict) -> float | None:
    """Same-instant underlying price from the chain's underlying block."""
    u = raw.get("underlying") or {}
    last = u.get("last")
    return float(last) if isinstance(last, (int, float)) else None


def _iter_puts(raw: dict) -> list[dict]:
    """Flatten ``putExpDateMap`` keeping the quote fields we need."""
    out: list[dict] = []
    for expiry_key, strike_map in (raw.get("putExpDateMap") or {}).items():
        expiry, _, dte_part = expiry_key.partition(":")
        try:
            dte = int(dte_part)
        except ValueError:
            continue
        for _strike, rows in (strike_map or {}).items():
            for row in rows or []:
                out.append(
                    {
                        "expiry": expiry,
                        "dte": dte,
                        "strike": row.get("strikePrice"),
                        "delta": row.get("delta"),
                        "bid": row.get("bid"),
                        "ask": row.get("ask"),
                        "open_interest": row.get("openInterest"),
                        "volume": row.get("totalVolume"),
                    }
                )
    return out


def is_third_friday(iso_date: str) -> bool:
    """True if the ISO date is the 3rd Friday of its month (monthly expiry)."""
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return False
    return d.weekday() == 4 and 15 <= d.day <= 21


def pick_target_expiry(puts: list[dict]) -> tuple[str, int] | None:
    """Choose the (expiry, dte) nearest ``TARGET_DTE`` within [DTE_LO, DTE_HI].

    Standard-monthly expiries (3rd Friday) are preferred; only when no
    monthly falls in the window do we consider weeklies.
    """
    in_window = {
        (p["expiry"], p["dte"])
        for p in puts
        if isinstance(p["dte"], int) and DTE_LO <= p["dte"] <= DTE_HI
    }
    if not in_window:
        return None
    monthly = {pair for pair in in_window if is_third_friday(pair[0])}
    pool = monthly or in_window
    return min(pool, key=lambda pair: abs(pair[1] - TARGET_DTE))


def locate_target_put(raw: dict) -> tuple[TargetPut | None, str | None]:
    """Return the target put and ``None``, or ``(None, reason)`` on failure.

    Reasons: ``no_puts``, ``no_expiry_in_window``, ``no_delta``. Bid/ask
    validity and the delta plausibility band are assessed by the caller
    (data-quality guards) so the located row is still recorded for
    diagnosis rather than silently dropped here.
    """
    puts = _iter_puts(raw)
    if not puts:
        return None, "no_puts"
    picked = pick_target_expiry(puts)
    if picked is None:
        return None, "no_expiry_in_window"
    expiry, dte = picked
    candidates = [
        p
        for p in puts
        if p["expiry"] == expiry
        and isinstance(p.get("delta"), (int, float))
        and p.get("strike") is not None
    ]
    if not candidates:
        return None, "no_delta"
    best = min(candidates, key=lambda p: abs(p["delta"] - TARGET_PUT_DELTA))
    bid = _f(best.get("bid"))
    ask = _f(best.get("ask"))
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    spread_pct = (
        (ask - bid) / mid if (mid is not None and mid > 0 and bid is not None) else None
    )
    return (
        TargetPut(
            expiry=expiry,
            dte=dte,
            strike=float(best["strike"]),
            delta=float(best["delta"]),
            bid=bid if bid is not None else 0.0,
            ask=ask if ask is not None else 0.0,
            mid=mid if mid is not None else 0.0,
            open_interest=int(best.get("open_interest") or 0),
            volume=int(best.get("volume") or 0),
            spread_pct=spread_pct,
        ),
        None,
    )


def _f(x: object) -> float | None:
    return float(x) if isinstance(x, (int, float)) else None
