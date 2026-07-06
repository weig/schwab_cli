"""Forward realized volatility (plan §2.3 / §7 / Stage F).

The ground truth the ranking is validated against: the realized vol that
actually occurred in the 21 trading days AFTER a snapshot. Reuses the same
estimator (`analytics.vol.realized_vol`, log returns × √252) the screener
uses for HV30 so history and forward measure agree.
"""
from __future__ import annotations

from schwab_cli.analytics.vol import realized_vol

FORWARD_WINDOW = 21  # trading days


def forward_rv(closes_from_anchor: list[float]) -> float | None:
    """Annualized realized vol over the forward window.

    ``closes_from_anchor`` is the daily-close series starting at the snapshot
    day (the anchor) and spanning the following trading days — needs the
    anchor plus ``FORWARD_WINDOW`` closes (22 points) to yield 21 returns.
    Returns None if the window hasn't fully elapsed yet.
    """
    if len(closes_from_anchor) < FORWARD_WINDOW + 1:
        return None
    window = closes_from_anchor[: FORWARD_WINDOW + 1]
    return realized_vol(window, window=FORWARD_WINDOW)
