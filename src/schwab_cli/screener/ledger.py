"""Paper-ledger math (plan §7) — cohort selection + settle PnL.

Pure helpers. The ledger records top-N and bottom-N virtual 1-contract put
sales daily and settles them at expiry with no roll / no early close, so the
monthly report measures the ranking's raw discrimination (top vs bottom),
not management skill.
"""
from __future__ import annotations

from schwab_cli.screener.config import ScreenerConfig


def settle_pnl(premium_bid: float, strike: float, s_expiry: float) -> float:
    """Hold-to-expiry PnL of a short put (per share): premium kept minus
    assignment loss ``max(strike - S_expiry, 0)``."""
    return premium_bid - max(strike - s_expiry, 0.0)


def select_cohorts(
    ranked: list[dict], cfg: ScreenerConfig
) -> list[tuple[str, dict]]:
    """Return ``[(cohort, row), ...]`` for the top-N and bottom-N survivors.

    ``ranked`` is the rank-ordered survivor list from
    :func:`schwab_cli.screener.ranking.rank_survivors`. With fewer than
    ``2*N`` survivors the two cohorts would overlap; to keep top and bottom
    disjoint we split what's available down the middle and never place the
    same symbol in both.
    """
    n = cfg.cohort_size
    total = len(ranked)
    if total == 0:
        return []
    if total < 2 * n:
        half = total // 2
        top = ranked[:half]
        bottom = ranked[total - half:]
    else:
        top = ranked[:n]
        bottom = ranked[-n:]
    out: list[tuple[str, dict]] = [("top", r) for r in top]
    out += [("bottom", r) for r in bottom]
    return out
