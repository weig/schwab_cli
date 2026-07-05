"""Paper-ledger validation report (plan §7).

The gate on the whole screener: does the ranking discriminate? We compare
the top cohort against the bottom cohort using the **paired daily
top-minus-bottom spread** — pairing on the open date differences out the
market beta both cohorts share, so we measure selection, not direction.
Because hold-to-expiry short-put PnL is fat-tailed and same-day positions
are cross-sectionally correlated, significance is judged with a bootstrap
CI over the daily spreads, not a naive t-test.
"""
from __future__ import annotations

import random
from statistics import fmean


def cohort_summary(rows: list) -> dict:
    """Per-cohort {n, mean_pnl, win_rate} over settled rows."""
    out: dict[str, dict] = {}
    for cohort in ("top", "bottom"):
        pnls = [
            r["pnl"] for r in rows
            if r["cohort"] == cohort and r["settled_at"] is not None
            and r["pnl"] is not None
        ]
        out[cohort] = {
            "n": len(pnls),
            "mean_pnl": fmean(pnls) if pnls else None,
            "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
        }
    return out


def daily_paired_spreads(rows: list) -> list[float]:
    """Per open_date: mean(top pnl) − mean(bottom pnl), for days with both."""
    by_date: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        if r["settled_at"] is None or r["pnl"] is None:
            continue
        slot = by_date.setdefault(r["open_date"], {"top": [], "bottom": []})
        if r["cohort"] in slot:
            slot[r["cohort"]].append(r["pnl"])
    spreads: list[float] = []
    for _date in sorted(by_date):
        top = by_date[_date]["top"]
        bottom = by_date[_date]["bottom"]
        if top and bottom:
            spreads.append(fmean(top) - fmean(bottom))
    return spreads


def bootstrap_ci(
    spreads: list[float], *, iters: int = 2000, alpha: float = 0.05,
    rng: random.Random | None = None,
) -> tuple[float, float, float] | None:
    """Mean paired spread and a (1-alpha) percentile bootstrap CI.

    Returns (mean, lo, hi) or None when there's too little data. A CI whose
    lower bound is > 0 is evidence the top cohort genuinely beats the bottom.
    """
    n = len(spreads)
    if n < 2:
        return None
    rng = rng or random.Random()
    means = []
    for _ in range(iters):
        sample = [spreads[rng.randrange(n)] for _ in range(n)]
        means.append(fmean(sample))
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return fmean(spreads), lo, hi


def ledger_report(rows: list, *, rng: random.Random | None = None) -> dict:
    """Full §7 report: cohort summaries + paired-spread bootstrap verdict."""
    spreads = daily_paired_spreads(rows)
    ci = bootstrap_ci(spreads, rng=rng)
    discriminates = ci is not None and ci[1] > 0
    return {
        "cohorts": cohort_summary(rows),
        "paired_days": len(spreads),
        "mean_spread": ci[0] if ci else None,
        "ci95": [ci[1], ci[2]] if ci else None,
        "discriminates": discriminates,
        "note": (
            "top significantly beats bottom (95% CI lower bound > 0)"
            if discriminates
            else "insufficient evidence — do NOT conclude yet (low power early on)"
        ),
    }
