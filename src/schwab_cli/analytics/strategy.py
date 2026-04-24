"""Core analytics for the ``strategy`` command — pure math, no I/O.

Closed-form metrics under a log-normal terminal-price density:

* :func:`payoff_at_expiry` — piecewise-linear payoff in dollars per
  spread (already ×100 per contract).
* :func:`breakevens` — analytic piecewise-linear zero-crossings.
* :func:`max_profit`, :func:`max_loss` — vertex scan + asymptote
  slopes; returns ``None`` for unbounded P/L.
* :func:`pop` — probability of profit via breakeven interval
  decomposition.
* :func:`ev` — expected P/L via truncated log-normal first moment.
* :func:`prob_touch` — single-barrier one-touch via reflection
  principle (assumes zero drift for the reflection).
* :func:`combined_greeks` — additive aggregation of per-leg Δ/Γ/Θ/ν.

All inputs are :class:`PricedLeg` — a parsed leg enriched with premium
paid/received, IV used for probability weighting, and greeks pulled
from the chain. Phase-1 scope is single-expiry only; multi-expiry
shapes fall back to ``supported=False`` in the classifier before they
reach this module.

Conventions:

* ``premium`` is per-share, always positive. Sign is applied via ``qty``.
* Payoff and EV are in **dollars per spread**, ×100 multiplier already
  applied (equity options assumed).
* Strike / spot are dollar prices.
* ``iv`` is decimal (0.30 = 30%).
* ``dte`` is integer days; the module clamps to ``T = max(dte/365, 1e-6)``
  internally to avoid σ√T = 0 CDF degeneracy. The 0-DTE case is
  short-circuited to ``payoff(spot) > 0 ? 1 : 0`` for POP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

Side = Literal["C", "P"]

# Equity options use a 100-share multiplier. Keep as a named constant
# in case we later extend to index options with different multipliers.
_MULTIPLIER = 100

# DTE floor — prevents σ√T = 0 at 0-DTE from blowing up the CDFs.
_T_MIN = 1e-6


@dataclass(frozen=True)
class PricedLeg:
    """Parsed leg enriched with premium, IV, and greeks from the chain."""

    qty: int
    side: Side
    expiry: date
    strike: float
    premium: float
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


# ---- elementary helpers ------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _t_years(dte: int) -> float:
    return max(dte / 365.0, _T_MIN)


def _d2(spot: float, K: float, sigma: float, T: float, r: float) -> float:
    return (math.log(spot / K) + (r - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def _d1(spot: float, K: float, sigma: float, T: float, r: float) -> float:
    return _d2(spot, K, sigma, T, r) + sigma * math.sqrt(T)


# ---- payoff ------------------------------------------------------------


def _leg_intrinsic(leg: PricedLeg, S: float) -> float:
    """Intrinsic value per share at underlying price ``S``."""
    if leg.side == "C":
        return max(0.0, S - leg.strike)
    return max(0.0, leg.strike - S)


def payoff_at_expiry(legs: list[PricedLeg], S: float) -> float:
    """Dollars P/L per spread at underlying price ``S`` (after ×100
    multiplier). ``qty`` is signed: negative = short.

    For a long leg we paid ``premium`` per share and receive ``intrinsic``.
    For a short leg we received ``premium`` and owe ``intrinsic``. Both
    are captured by ``qty × (intrinsic - premium)``.
    """
    total_per_share = 0.0
    for leg in legs:
        total_per_share += leg.qty * (_leg_intrinsic(leg, S) - leg.premium)
    return total_per_share * _MULTIPLIER


# ---- slope helpers (for asymptotes and segment-linear computations) ---


def _slope_at(legs: list[PricedLeg], S: float) -> float:
    """Slope of payoff(S) wrt S at price ``S``, per share (before ×100).

    Each call contributes +qty for S > strike (ITM) and 0 below. Each
    put contributes -qty for S < strike and 0 above. At kinks (S == K)
    we use the right-limit — doesn't matter for segment interiors.
    """
    slope = 0.0
    for leg in legs:
        if leg.side == "C":
            if S > leg.strike:
                slope += leg.qty
        else:  # put
            if S < leg.strike:
                slope -= leg.qty
    return slope


def _right_asymptote_slope(legs: list[PricedLeg]) -> float:
    """Slope as ``S → +∞``: only calls contribute. Per-share slope."""
    return float(sum(leg.qty for leg in legs if leg.side == "C"))


def _left_asymptote_slope(legs: list[PricedLeg]) -> float:
    """Slope as ``S → 0+``: only puts contribute (negated sign from
    (K - S) intrinsic). Per-share slope in S."""
    return float(sum(-leg.qty for leg in legs if leg.side == "P"))


# ---- breakevens --------------------------------------------------------


def _kinks(legs: list[PricedLeg]) -> list[float]:
    """Sorted unique strike values — the payoff function is linear
    between these breakpoints."""
    return sorted({leg.strike for leg in legs})


def breakevens(legs: list[PricedLeg]) -> list[float]:
    """Zero-crossings of payoff(S) on (0, +∞), sorted ascending.

    Payoff is piecewise-linear with kinks at each strike. For each
    segment with opposite-sign endpoints we solve the linear equation
    payoff(a) + slope·(x - a) = 0. Handles unbounded-right segments
    via the right asymptote slope.
    """
    kinks = _kinks(legs)
    if not kinks:
        return []

    # Sample points: S=0, every kink, and a point well beyond the
    # rightmost strike ("far right" — big enough that the asymptote
    # dominates, but use the slope to solve exactly rather than sample).
    result: list[float] = []

    # Left edge: (0, kinks[0]).
    p0 = payoff_at_expiry(legs, 0.0)
    p_first = payoff_at_expiry(legs, kinks[0])
    if p0 == 0.0:
        result.append(0.0)
    if (p0 > 0 > p_first) or (p0 < 0 < p_first):
        # Linear between S=0 and kinks[0].
        if p_first != p0:
            x = -p0 * kinks[0] / (p_first - p0)
            if 0 < x < kinks[0]:
                result.append(x)

    # Interior segments.
    for a, b in zip(kinks, kinks[1:]):
        pa = payoff_at_expiry(legs, a)
        pb = payoff_at_expiry(legs, b)
        if pa == 0.0 and a not in result:
            result.append(a)
        if (pa > 0 > pb) or (pa < 0 < pb):
            if pb != pa:
                x = a + (-pa) * (b - a) / (pb - pa)
                if a < x < b:
                    result.append(x)

    # Right edge: beyond the last kink. Use asymptote slope.
    p_last = payoff_at_expiry(legs, kinks[-1])
    if p_last == 0.0 and kinks[-1] not in result:
        result.append(kinks[-1])
    right_slope = _right_asymptote_slope(legs) * _MULTIPLIER  # dollars/$
    if right_slope != 0:
        # Solve p_last + right_slope × (x - kinks[-1]) = 0 → x = kinks[-1] - p_last / slope.
        # Only if that x is > kinks[-1] AND the sign actually crosses.
        if (p_last > 0 and right_slope < 0) or (p_last < 0 and right_slope > 0):
            x = kinks[-1] - p_last / right_slope
            if x > kinks[-1]:
                result.append(x)

    return sorted(set(round(x, 10) for x in result))


# ---- max profit / max loss --------------------------------------------


def max_profit(legs: list[PricedLeg]) -> float | None:
    """Maximum P/L; returns ``None`` if unbounded above.

    Evaluates payoff at each kink and at S=0, then consults the left
    and right asymptote slopes. Unbounded ⇒ ``None``; otherwise the
    largest finite value.
    """
    if _right_asymptote_slope(legs) > 0:
        return None
    # Left asymptote: we only go down to S=0 (not -∞), so even a
    # negative left slope just means payoff grows toward S=0 and we
    # capture it by evaluating at S=0 directly.
    return _vertex_max(legs, direction="max")


def max_loss(legs: list[PricedLeg]) -> float | None:
    """Minimum (most-negative) P/L; returns ``None`` if unbounded below."""
    if _right_asymptote_slope(legs) < 0:
        return None
    # At S=0 puts cap their value at K (bounded), so left side is always finite.
    return _vertex_max(legs, direction="min")


def _vertex_max(
    legs: list[PricedLeg], *, direction: Literal["max", "min"]
) -> float:
    """Return the max (or min) of payoff evaluated at the kink vertices
    plus S=0 and a far-right sentinel for flat-slope asymptotes."""
    kinks = _kinks(legs)
    candidates: list[float] = []
    for S in [0.0, *kinks]:
        candidates.append(payoff_at_expiry(legs, S))
    # Far-right sentinel for zero-slope right asymptote: use any point
    # beyond the max strike; value will match the last kink.
    if _right_asymptote_slope(legs) == 0 and kinks:
        candidates.append(payoff_at_expiry(legs, kinks[-1] + 1.0))
    return max(candidates) if direction == "max" else min(candidates)


# ---- POP ---------------------------------------------------------------


def _iv_for_leg(leg: PricedLeg, fallback: float | None) -> float | None:
    return leg.iv if leg.iv is not None else fallback


def _anchor_iv(legs: list[PricedLeg]) -> float:
    """Resolve the σ for the log-normal density — arithmetic mean of
    per-leg IVs. A log-normal density carries one σ by construction,
    so skew-aware probabilities would require a non-lognormal density
    (Phase 2)."""
    ivs = [leg.iv for leg in legs if leg.iv is not None]
    if not ivs:
        return 0.0
    return sum(ivs) / len(ivs)


def pop(
    legs: list[PricedLeg],
    *,
    spot: float,
    dte: int,
    r: float = 0.0,
) -> float:
    """Probability of profit at expiry under log-normal(S_0, σ√T).

    Steps: compute breakevens → partition (0, +∞) at those breakevens
    → identify profitable intervals by sampling midpoints → sum the
    log-normal measure of each profitable interval.

    Degenerate cases:

    * ``dte == 0`` → deterministic: 1.0 if currently profitable else 0.0.
    * ``σ`` resolves to 0 (no IV on any leg) → same deterministic rule,
      evaluated at ``spot``.
    """
    if dte <= 0:
        return 1.0 if payoff_at_expiry(legs, spot) > 0 else 0.0

    sigma = _anchor_iv(legs)
    if sigma <= 0:
        return 1.0 if payoff_at_expiry(legs, spot) > 0 else 0.0

    T = _t_years(dte)
    bes = breakevens(legs)

    # Interval endpoints: 0+, breakevens, +∞.
    edges = [0.0, *bes, math.inf]

    total = 0.0
    for a, b in zip(edges, edges[1:]):
        # Sample interior to classify profitable/not. Choose a point
        # that's definitely inside this interval.
        if math.isinf(b):
            sample = max(a + 1.0, spot * 2.0)
        else:
            sample = (a + b) / 2.0 if a > 0 else b / 2.0
        if payoff_at_expiry(legs, sample) <= 0:
            continue
        total += _lognormal_interval_prob(a, b, spot, sigma, T, r)

    # Float drift could push just over 1.0 or under 0.0 — clamp.
    return max(0.0, min(1.0, total))


def _lognormal_interval_prob(
    a: float, b: float, spot: float, sigma: float, T: float, r: float
) -> float:
    """P(a < S_T < b) under log-normal(ln S_0 + (r - σ²/2)T, σ²T)."""
    # P(S_T > K) = N(d2(K)).
    # P(a < S_T < b) = P(S_T > a) - P(S_T > b) = N(d2(a)) - N(d2(b)).
    p_above_a = _norm_cdf(_d2(spot, a, sigma, T, r)) if a > 0 else 1.0
    p_above_b = _norm_cdf(_d2(spot, b, sigma, T, r)) if math.isfinite(b) else 0.0
    return max(0.0, p_above_a - p_above_b)


# ---- EV ----------------------------------------------------------------


def ev(
    legs: list[PricedLeg],
    *,
    spot: float,
    dte: int,
    r: float = 0.0,
) -> float:
    """Expected P/L in dollars per spread, under the same log-normal
    density POP uses.

    Partition (0, +∞) at the kinks (strikes) — payoff is linear on each
    segment with slope ``m`` and intercept ``c`` — then integrate::

        E[payoff] = Σ_segments  m · E[S · 1_{a<S<b}]  +  c · P(a<S<b)

    with::

        E[S · 1_{a<S<b}] = spot · e^(rT) · (N(d1(a)) - N(d1(b)))
        P(a<S<b)         =                 (N(d2(a)) - N(d2(b)))

    Converted to dollars via the ``×100`` multiplier already embedded in
    ``payoff_at_expiry``.
    """
    if dte <= 0:
        return payoff_at_expiry(legs, spot)

    sigma = _anchor_iv(legs)
    if sigma <= 0:
        return payoff_at_expiry(legs, spot)

    T = _t_years(dte)
    kinks = _kinks(legs)

    # Per-share slope and intercept on each segment. Segments:
    # (0, k1), (k1, k2), ..., (k_n, +∞). Use a representative midpoint
    # or beyond-k_n sentinel to read slope/intercept.
    edges: list[float] = [0.0, *kinks, math.inf]
    total = 0.0
    for a, b in zip(edges, edges[1:]):
        if math.isinf(b):
            sample = max(a + 1.0, kinks[-1] + 1.0 if kinks else a + 1.0)
        else:
            sample = (a + b) / 2.0 if a > 0 else b / 2.0
        # Slope (per share) and intercept at sample.
        m_per_share = _slope_at(legs, sample)
        p_sample = payoff_at_expiry(legs, sample) / _MULTIPLIER  # per share
        c_per_share = p_sample - m_per_share * sample

        # P(a < S < b) under log-normal.
        p_above_a = _norm_cdf(_d2(spot, a, sigma, T, r)) if a > 0 else 1.0
        p_above_b = _norm_cdf(_d2(spot, b, sigma, T, r)) if math.isfinite(b) else 0.0
        seg_prob = max(0.0, p_above_a - p_above_b)

        # E[S · 1_{a<S<b}] under log-normal.
        drift = math.exp(r * T)
        e1_a = _norm_cdf(_d1(spot, a, sigma, T, r)) if a > 0 else 1.0
        e1_b = _norm_cdf(_d1(spot, b, sigma, T, r)) if math.isfinite(b) else 0.0
        e_s_seg = spot * drift * max(0.0, e1_a - e1_b)

        total += m_per_share * e_s_seg + c_per_share * seg_prob

    return total * _MULTIPLIER


# ---- prob_touch --------------------------------------------------------


def prob_touch(*, K: float, spot: float, iv: float, dte: int, r: float = 0.0) -> float:
    """Probability that the underlying touches barrier ``K`` at least
    once before expiry.

    Zero-drift reflection-principle closed form::

        P(touch K) = 2 · N(-|ln(K/S_0)| / (σ√T))

    Assumes ``r = 0`` for the reflection symmetry; non-zero drift
    requires the drifted Brownian formula which is a strict upgrade but
    out of scope for MVP. The result is clamped to [0, 1].

    If ``K`` equals ``spot``, the barrier is already touched at t=0,
    so we return 1.0.
    """
    if K == spot:
        return 1.0
    if dte <= 0 or iv <= 0:
        # Without time or vol, no further motion — touch iff already there.
        return 1.0 if K == spot else 0.0
    T = _t_years(dte)
    log_dist = abs(math.log(K / spot))
    arg = -log_dist / (iv * math.sqrt(T))
    # r is accepted for signature symmetry with the other functions; for
    # MVP the zero-drift reflection is what we document and compute.
    _ = r
    return max(0.0, min(1.0, 2.0 * _norm_cdf(arg)))


# ---- combined greeks ---------------------------------------------------


def combined_greeks(legs: list[PricedLeg]) -> dict[str, float | None]:
    """Sum per-leg Δ/Γ/Θ/ν, respecting sign via ``qty``.

    * ``delta``: shares-equivalent exposure per spread.
    * ``gamma``: shares-per-dollar per spread.
    * ``theta``: dollars/day per spread (×100 from per-share).
    * ``vega``: dollars/vol-pt per spread (×100 from per-share).

    Returns ``None`` for any greek if *any* leg is missing that field —
    a partial sum would under-state the real exposure and mislead.
    """
    result: dict[str, float | None] = {}
    for key, multiplier in (
        ("delta", 1),
        ("gamma", 1),
        ("theta", _MULTIPLIER),
        ("vega", _MULTIPLIER),
    ):
        values = [getattr(leg, key) for leg in legs]
        if any(v is None for v in values):
            result[key] = None
            continue
        total = sum(leg.qty * getattr(leg, key) for leg in legs)
        result[key] = total * multiplier
    return result
