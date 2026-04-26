"""Volatility analytics — pure math, no I/O.

Provides the building blocks for the ``vol`` command:

* :func:`log_returns` and :func:`realized_vol` for historical volatility.
* :func:`rolling_realized_vol` for an HV series over time (feeds HVP).
* :func:`percentile_rank` for ranking today's value in a historical series.
* :func:`aggregate_pc` for put/call ratios across a chain response.
* :func:`pick_atm_contract` for selecting the ATM strike + expiry whose
  IV we surface in the ``vol`` output (and whose daily value we'll snapshot
  in phase 2 for real IVP).

All functions are deterministic and have no external dependencies. HV is
annualised assuming 252 trading days per year — the industry convention
for US equity options.
"""

from __future__ import annotations

import math
from statistics import stdev
from typing import Any

_TRADING_DAYS_PER_YEAR = 252


# ---- log returns + HV ---------------------------------------------------


def log_returns(closes: list[float]) -> list[float]:
    """Return ``ln(C_t / C_{t-1})`` for consecutive closes."""
    if len(closes) < 2:
        return []
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def realized_vol(closes: list[float], window: int = 30) -> float | None:
    """Annualised realised volatility from the last ``window`` sessions.

    Uses the sample standard deviation of log returns × √252. Returns
    ``None`` when there aren't enough closes to compute the requested
    window (need ``window + 1`` closes to form ``window`` returns).
    """
    if len(closes) < window + 1:
        return None
    rets = log_returns(closes[-(window + 1) :])
    if len(rets) < 2:
        return None
    return stdev(rets) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def rolling_realized_vol(closes: list[float], window: int = 30) -> list[float]:
    """HV series — one value per session where a full ``window`` precedes.

    For ``n`` closes, returns ``n - window`` values: the HV computed on
    sessions ``[0..window]``, ``[1..window+1]``, and so on, ending with
    the HV on the final ``window + 1`` closes (= :func:`realized_vol`).
    """
    if len(closes) < window + 1:
        return []
    rets = log_returns(closes)
    out: list[float] = []
    # Each output covers exactly `window` consecutive returns.
    for end in range(window, len(rets) + 1):
        window_rets = rets[end - window : end]
        out.append(stdev(window_rets) * math.sqrt(_TRADING_DAYS_PER_YEAR))
    return out


# ---- percentile rank ----------------------------------------------------


def percentile_rank(series: list[float], value: float) -> float:
    """Return the percentile rank of ``value`` within ``series``, in [0, 100].

    Uses midrank for ties: a value tied with ``k`` entries of the series
    gets credit for half of them. Empty series returns 0.
    """
    if not series:
        return 0.0
    below = sum(1 for v in series if v < value)
    equal = sum(1 for v in series if v == value)
    rank = below + 0.5 * equal
    return 100.0 * rank / len(series)


# ---- put/call aggregation ----------------------------------------------


def aggregate_pc(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate put/call volume and open interest across a contract list.

    Each contract is expected to carry ``side`` (``"C"`` or ``"P"``),
    ``volume`` (int/float/None), and ``openInterest`` (int/float/None).
    Unknown sides are ignored. ``None`` / missing fields count as zero.

    Returns a dict with per-side totals and the puts/calls ratio for
    volume and OI. Ratios are ``None`` when the call side is zero so
    callers can render a dash rather than printing infinity.
    """
    call_vol = put_vol = call_oi = put_oi = 0
    for c in contracts:
        vol = c.get("volume") or 0
        oi = c.get("openInterest") or 0
        side = c.get("side")
        if side == "C":
            call_vol += vol
            call_oi += oi
        elif side == "P":
            put_vol += vol
            put_oi += oi
    return {
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "volume_ratio": (put_vol / call_vol) if call_vol else None,
        "oi_ratio": (put_oi / call_oi) if call_oi else None,
    }


# ---- ATM picker --------------------------------------------------------


def pick_atm_contract(
    expiries: list[dict[str, Any]],
    spot: float,
    *,
    min_volume: int = 100,
) -> dict[str, Any] | None:
    """Pick the ATM contract for the current IV snapshot.

    Walks expiries in DTE order and returns the first one where:

      * Total call + put volume on the expiry is at least ``min_volume``.
        (Defends against zero-volume weeklies with stale quotes.)
      * A strike exists close to ``spot``.
      * At least one of the call or put at that strike has a non-None IV.

    Returns ``{"expiry", "dte", "strike", "iv"}`` — ``iv`` is the midpoint
    of the call and put IV at the chosen strike (if both present) or the
    single-leg IV (if only one is present). Returns ``None`` if no
    expiry qualifies.
    """
    for exp in sorted(expiries, key=lambda e: e.get("dte", 10_000)):
        contracts = exp.get("contracts", [])
        total_vol = sum((c.get("volume") or 0) for c in contracts)
        if total_vol < min_volume:
            continue

        by_strike: dict[float, list[dict]] = {}
        for c in contracts:
            strike = c.get("strike")
            if strike is None:
                continue
            by_strike.setdefault(strike, []).append(c)
        if not by_strike:
            continue

        atm_strike = min(by_strike.keys(), key=lambda s: abs(s - spot))
        ivs = [c["iv"] for c in by_strike[atm_strike] if c.get("iv") is not None]
        if not ivs:
            continue

        return {
            "expiry": exp.get("expiry"),
            "dte": exp.get("dte"),
            "strike": atm_strike,
            "iv": sum(ivs) / len(ivs),
        }
    return None


# ---- term-structure interpolation -------------------------------------


def interp_iv_in_variance(
    curve: list[tuple[int, float]],
    target_dte: int,
) -> float | None:
    """Variance-linear interpolation between bracketing expiries.

    ``curve`` is ``[(dte, iv), ...]`` sorted by ``dte``; ``iv`` is a
    decimal (0.34 = 34%). Returns ``None`` if ``target_dte`` is outside
    ``[curve[0][0], curve[-1][0]]`` — we never extrapolate.

    The interpolation is linear in implied variance ``v(t) = iv² · t``.
    At target ``t``: ``v(t) = lerp(v(d_lo), v(d_hi), t)``, then
    ``iv(t) = sqrt(v(t) / t)``. This is the standard term-structure
    interpolation; linear-in-IV biases the front.
    """
    if not curve:
        return None
    if target_dte < curve[0][0] or target_dte > curve[-1][0]:
        return None
    for i in range(len(curve)):
        d, iv = curve[i]
        if d == target_dte:
            return iv
        if i == 0:
            continue
        d_prev, iv_prev = curve[i - 1]
        if d_prev <= target_dte <= d:
            v_lo = iv_prev * iv_prev * d_prev
            v_hi = iv * iv * d
            v_t = v_lo + (v_hi - v_lo) * (target_dte - d_prev) / (d - d_prev)
            return math.sqrt(v_t / target_dte)
    return None
