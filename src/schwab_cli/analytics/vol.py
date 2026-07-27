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
_RISK_FREE_RATE_FALLBACK = 0.05  # 3-month T-bill approximation


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


def _expiry_liquidity(contracts: list[dict]) -> float:
    """Sum of volume + open interest across a contract list.

    Volume alone resets to zero on weekends / pre-market, which made
    the cron's gate reject every expiry on Sunday-evening runs even
    for SPX heavyweights. Open interest carries over between sessions
    and is the persistent liquidity signal — adding the two gives a
    threshold that survives session boundaries.
    """
    return sum(
        (c.get("volume") or 0) + (c.get("openInterest") or 0)
        for c in (contracts or [])
    )


def pick_atm_contract(
    expiries: list[dict[str, Any]],
    spot: float,
    *,
    min_liquidity: int = 100,
    min_dte: int = 7,
) -> dict[str, Any] | None:
    """Pick the ATM contract for the current IV snapshot.

    Expiries at/beyond ``min_dte`` are preferred (nearest first) so the
    headline ATM IV isn't read off a 0-DTE contract, whose IV is dominated by
    expiration-day gamma/pin/theta noise and swings wildly day-to-day. Only if
    no in-window expiry qualifies do we fall back to nearer ones (closest to
    ``min_dte`` first). The 30-day constant-maturity ``atm_iv_30d`` remains the
    stable series for IVR; this just keeps the *displayed / stored* near-term
    IV off the expiring contract.

    Walks expiries in preference order and returns the first one where:

      * Total call + put volume *plus* open interest on the expiry is
        at least ``min_liquidity``. We sum volume and OI so weekend /
        pre-market runs still qualify on Friday's open interest even
        when today's volume is zero. Defends against truly stale
        weeklies (zero everywhere).
      * A strike exists close to ``spot``.
      * At least one of the call or put at that strike has a non-None IV.

    Returns ``{"expiry", "dte", "strike", "iv"}`` — ``iv`` is the midpoint
    of the call and put IV at the chosen strike (if both present) or the
    single-leg IV (if only one is present). Returns ``None`` if no
    expiry qualifies.
    """
    def _pref(e: dict[str, Any]) -> tuple[int, int]:
        # (0, dte) for dte >= min_dte (nearest first); (1, -dte) below the
        # window so 6-DTE beats 0-DTE when nothing in-window qualifies.
        dte = int(e.get("dte") or 0)
        return (0, dte) if dte >= min_dte else (1, -dte)

    for exp in sorted(expiries, key=_pref):
        contracts = exp.get("contracts", [])
        if _expiry_liquidity(contracts) < min_liquidity:
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


# ---- contract validity -----------------------------------------------


def is_valid_contract(c: dict) -> bool:
    """Reject Schwab's empty-quote sentinels before a contract feeds any
    IV computation.

    Schwab returns ``iv = -999.0`` (stored as ``-9.99``) and ``delta =
    -999`` on strikes with no live market, and ``bid``/``ask`` of ``0``.
    The IV sentinel is the dangerous one: :func:`interp_iv_in_variance`
    squares IV, so ``-9.99`` becomes a huge POSITIVE variance that then
    passes every downstream ``iv > 0`` check (this is the GOOG 2026-05-17
    ``atm_iv_30d = 8.43`` incident). A usable ``iv`` is required (missing
    or non-positive → invalid); ``delta`` and ``bid``/``ask`` are checked
    only when present, since curve/skew inputs legitimately omit quotes.
    """
    iv = c.get("iv")
    if iv is None or iv <= 0:
        return False
    delta = c.get("delta")
    if delta is not None and delta <= -999:
        return False
    bid, ask = c.get("bid"), c.get("ask")
    if bid is not None and bid <= 0:
        return False
    if ask is not None and ask <= 0:
        return False
    return True


# ---- ATM curve builder ------------------------------------------------


def pick_atm_curve(
    expiries: list[dict],
    spot: float,
    *,
    min_liquidity: int = 100,
) -> list[tuple[int, float]]:
    """Build the ``[(dte, atm_iv), ...]`` curve from a chain.

    Skips expiries whose total ``volume + openInterest`` is below
    ``min_liquidity`` (same gate as :func:`pick_atm_contract` — sum
    keeps weekend / pre-market runs alive when volume is zero but OI
    isn't). For each kept expiry, picks the strike closest to ``spot``
    and returns the call/put IV midpoint (or single side if the other
    lacks IV). Sorted by DTE.
    """
    out: list[tuple[int, float]] = []
    for exp in expiries:
        contracts = exp.get("contracts") or []
        if _expiry_liquidity(contracts) < min_liquidity:
            continue
        by_strike: dict[float, list[dict]] = {}
        for c in contracts:
            s = c.get("strike")
            if s is None:
                continue
            by_strike.setdefault(s, []).append(c)
        if not by_strike:
            continue
        atm = min(by_strike.keys(), key=lambda s: abs(s - spot))
        # Only sane IVs: is_valid_contract rejects the -9.99 sentinel, which
        # would otherwise be SQUARED into a huge variance downstream.
        ivs = [c["iv"] for c in by_strike[atm]
               if c.get("iv") is not None and is_valid_contract(c)]
        if not ivs:
            continue
        out.append((int(exp.get("dte") or 0), sum(ivs) / len(ivs)))
    out.sort(key=lambda x: x[0])
    return out


# ---- wing tenor selector ---------------------------------------------


def closest_dte_expiry(
    expiries: list[dict],
    target_dte: int,
) -> dict | None:
    """Pick the expiry whose ``dte`` is closest to ``target_dte``.

    Used for 25Δ wing selection (we don't interpolate wings — picking
    25Δ across two interpolated tenors gets murky). Ties broken by
    lower DTE (more liquid).
    """
    if not expiries:
        return None
    return min(
        expiries,
        key=lambda e: (abs((e.get("dte") or 0) - target_dte), e.get("dte") or 0),
    )


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


# ---- 25Δ wing picker --------------------------------------------------


def pick_25d_wing(
    expiry: dict,
    *,
    side: str,
    target_delta: float,
    spot: float | None = None,
    atm_iv: float | None = None,
    max_delta_distance: float = 0.10,
) -> dict | None:
    """Pick the contract on ``side`` whose delta is closest to ``target_delta``.

    Reads delta from each contract directly. If a contract lacks delta
    AND ``spot`` and ``atm_iv`` are provided, BS-derive it (rate
    approximation taken from the existing :data:`vol._RISK_FREE_RATE_FALLBACK`).
    The fallback path is rarely needed — Schwab returns deltas.

    Returns ``{strike, expiry, dte, iv, delta}`` or ``None`` if no
    contract on ``side`` has ``|delta - target_delta| < max_delta_distance``.
    """
    contracts = expiry.get("contracts") or []
    candidates = [c for c in contracts if c.get("side") == side]
    scored: list[tuple[float, dict]] = []
    for c in candidates:
        delta = c.get("delta")
        if delta is None and spot is not None and atm_iv is not None:
            delta = _bs_delta_fallback(
                c, spot=spot, atm_iv=atm_iv,
                dte=expiry.get("dte") or 30,
                side=side,
            )
        if delta is None:
            continue
        scored.append((abs(delta - target_delta), {
            "strike":  c.get("strike"),
            "expiry":  expiry.get("expiry"),
            "dte":     expiry.get("dte"),
            "iv":      c.get("iv"),
            "delta":   delta,
        }))
    if not scored:
        return None
    distance, best = min(scored, key=lambda x: x[0])
    if distance >= max_delta_distance:
        return None
    return best


def _bs_delta_fallback(
    contract: dict,
    *,
    spot: float,
    atm_iv: float,
    dte: int,
    side: str,
) -> float | None:
    """BS delta with rate=_RISK_FREE_RATE_FALLBACK, dividend=0.

    Worst-case bias for a high-yielder is ~0.005 in delta — tolerable
    for picking the 25Δ wing, which has natural ±0.05 strike-spacing
    granularity anyway.
    """
    strike = contract.get("strike")
    if strike is None or strike <= 0 or spot <= 0 or atm_iv <= 0 or dte <= 0:
        return None
    t = dte / 365.0
    sigma_sqrt_t = atm_iv * math.sqrt(t)
    if sigma_sqrt_t <= 0:
        return None
    rate = _RISK_FREE_RATE_FALLBACK
    d1 = (math.log(spot / strike) + (rate + 0.5 * atm_iv ** 2) * t) / sigma_sqrt_t
    # N(d1) via erfc.
    n_d1 = 0.5 * math.erfc(-d1 / math.sqrt(2.0))
    return n_d1 if side == "C" else (n_d1 - 1.0)
