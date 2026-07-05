"""Ranking (plan §5) — a single economic metric, no weighted composite.

``executable_vrp`` = bid-side annualized put yield minus the fair yield a
Black-Scholes put priced at trailing HV30 would pay. It rewards real
bid-side premium over what historical vol says is fair, penalizing both fat
spreads (bid is depressed) and thin absolute premium. Survivors are ordered
by this metric; ties break by IVR then tighter spread — ordering only, never
folded into a score.
"""
from __future__ import annotations

from schwab_cli.analytics.bs import bs_price
from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.storage.screener import ContractSnapshot

_DAYS_PER_YEAR = 365.0


def compute_vrp(snap: ContractSnapshot, cfg: ScreenerConfig) -> dict | None:
    """Return {executable_vrp, premium_yield_bid, fair_yield} or None.

    None when a required input is missing/degenerate (no HV30, no bid, no
    strike, non-positive DTE) — such a row cannot be ranked.
    """
    if (
        snap.put_bid is None
        or snap.put_strike is None
        or snap.put_strike <= 0
        or snap.dte is None
        or snap.dte <= 0
        or snap.hv_30d is None
        or snap.hv_30d <= 0
        or snap.underlying_last is None
        or snap.underlying_last <= 0
    ):
        return None
    ann = _DAYS_PER_YEAR / snap.dte
    premium_yield_bid = snap.put_bid / snap.put_strike * ann
    fair_premium = bs_price(
        S=snap.underlying_last,
        K=snap.put_strike,
        T=snap.dte / _DAYS_PER_YEAR,
        r=cfg.rf_rate,
        sigma=snap.hv_30d,
        is_call=False,
    )
    fair_yield = fair_premium / snap.put_strike * ann
    return {
        "executable_vrp": premium_yield_bid - fair_yield,
        "premium_yield_bid": premium_yield_bid,
        "fair_yield": fair_yield,
    }


def rank_survivors(
    survivors: list[ContractSnapshot], cfg: ScreenerConfig
) -> list[dict]:
    """Rank survivors by executable_vrp desc; return ranking-row dicts.

    Tie-break (ordering only): higher IVR first, then tighter spread. Rows
    whose VRP can't be computed are dropped (they aren't rankable).
    """
    scored: list[dict] = []
    for snap in survivors:
        vrp = compute_vrp(snap, cfg)
        if vrp is None:
            continue
        scored.append(
            {
                "symbol": snap.symbol,
                "executable_vrp": vrp["executable_vrp"],
                "premium_yield_bid": vrp["premium_yield_bid"],
                "fair_yield": vrp["fair_yield"],
                "ivr": snap.ivr,
                "ivr_low_conf": snap.ivr_low_conf,
                "put_strike": snap.put_strike,
                "put_delta_actual": snap.put_delta_actual,
                "put_bid": snap.put_bid,
                "dte": snap.dte,
                "target_expiry": snap.target_expiry,
                "spread_pct": snap.spread_pct,
                "underlying_last": snap.underlying_last,
            }
        )
    scored.sort(
        key=lambda r: (
            -r["executable_vrp"],
            -(r["ivr"] if r["ivr"] is not None else -1.0),
            r["spread_pct"] if r["spread_pct"] is not None else 1.0,
        )
    )
    for i, row in enumerate(scored, start=1):
        row["rank"] = i
    return scored
