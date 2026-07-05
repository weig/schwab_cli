"""Tests for the paper-ledger validation report (§7)."""
from __future__ import annotations

import random

from schwab_cli.screener.report import (
    bootstrap_ci,
    cohort_summary,
    daily_paired_spreads,
    ledger_report,
)


def _row(open_date, cohort, pnl, settled=True):
    return {
        "open_date": open_date, "cohort": cohort, "pnl": pnl,
        "settled_at": 1 if settled else None,
    }


def test_cohort_summary_ignores_unsettled():
    rows = [
        _row("d1", "top", 2.0), _row("d1", "top", 0.0),
        _row("d1", "bottom", -1.0), _row("d2", "top", 3.0, settled=False),
    ]
    s = cohort_summary(rows)
    assert s["top"]["n"] == 2 and s["top"]["mean_pnl"] == 1.0
    assert s["top"]["win_rate"] == 0.5
    assert s["bottom"]["n"] == 1


def test_daily_paired_spreads_needs_both_cohorts():
    rows = [
        _row("d1", "top", 2.0), _row("d1", "bottom", 0.5),   # spread 1.5
        _row("d2", "top", 1.0),                              # no bottom -> skipped
        _row("d3", "top", 4.0), _row("d3", "bottom", 1.0),   # spread 3.0
    ]
    assert daily_paired_spreads(rows) == [1.5, 3.0]


def test_bootstrap_ci_positive_when_top_beats_bottom():
    spreads = [1.0, 1.2, 0.8, 1.1, 0.9, 1.3, 0.7, 1.0]
    ci = bootstrap_ci(spreads, iters=500, rng=random.Random(42))
    assert ci is not None
    mean, lo, hi = ci
    assert lo > 0 and lo <= mean <= hi


def test_bootstrap_ci_none_when_scarce():
    assert bootstrap_ci([1.0]) is None


def test_ledger_report_discriminates_flag():
    rows = []
    for i in range(8):
        rows.append(_row(f"d{i}", "top", 1.0 + 0.05 * i))
        rows.append(_row(f"d{i}", "bottom", 0.0))
    rep = ledger_report(rows, rng=random.Random(1))
    assert rep["paired_days"] == 8
    assert rep["discriminates"] is True
    assert rep["ci95"][0] > 0


def test_ledger_report_inconclusive_when_noisy():
    rows = [_row("d0", "top", 1.0), _row("d0", "bottom", 1.0)]  # 1 paired day
    rep = ledger_report(rows)
    assert rep["discriminates"] is False
    assert "do NOT conclude" in rep["note"]
