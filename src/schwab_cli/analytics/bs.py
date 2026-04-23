"""Black-Scholes pricing + Newton-Raphson implied-volatility solver.

Pure math, no I/O, no state. Matches the implementation we validated
live against Schwab's chain endpoint for NVDA 260501 C 202.5:

    computed IV 36.577%    Schwab IV 36.582%    (5 bps)

Used by the vol-history backfill to synthesise a 252-day IV series from
a single option's price history + the underlying's price history.
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``erf``."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, *, is_call: bool
) -> float:
    """Return the Black-Scholes price for a European option.

    * ``S``  spot
    * ``K``  strike
    * ``T``  time to expiry in years
    * ``r``  annualised risk-free rate
    * ``sigma``  annualised volatility
    """
    if T <= 0 or sigma <= 0:
        intrinsic = (S - K) if is_call else (K - S)
        return max(0.0, intrinsic)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if is_call:
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    *,
    is_call: bool,
    initial: float = 0.30,
    max_iter: int = 80,
    tolerance: float = 1e-8,
) -> float | None:
    """Newton-Raphson IV from an option's market price.

    Returns ``None`` when:

      * time is non-positive,
      * the market price is below intrinsic (BS has no solution),
      * the solver diverges / vega collapses (very deep ITM/OTM).
    """
    if T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price < intrinsic - 1e-9:
        return None

    sigma = initial
    for _ in range(max_iter):
        model = bs_price(S, K, T, r, sigma, is_call=is_call)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        vega = S * _norm_pdf(d1) * math.sqrt(T)
        if vega < 1e-10:
            return None
        diff = model - price
        if abs(diff) < tolerance:
            return sigma
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 0.01
    # Didn't converge — give back the last estimate if it's at least
    # positive; otherwise signal failure so the caller can skip the day.
    return sigma if sigma > 0 else None
