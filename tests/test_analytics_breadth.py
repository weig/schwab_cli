"""Breadth analytics: pure SMA comparison + aggregation."""
from __future__ import annotations

import pytest

from schwab_cli.analytics import breadth as B


def test_above_ma_true_when_last_close_exceeds_sma():
    # SMA(3) of [1, 2, 3] = 2; last close 3 > 2 → True
    assert B.above_ma([1.0, 2.0, 3.0], 3) is True


def test_above_ma_false_when_last_close_below_sma():
    assert B.above_ma([10.0, 10.0, 5.0], 3) is False


def test_above_ma_returns_none_when_insufficient_history():
    assert B.above_ma([1.0, 2.0], 5) is None


def test_above_ma_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        B.above_ma([1.0], 0)


def test_compute_breadth_excludes_short_histories_from_both_sides():
    """Coverage signal must reflect symbols actually evaluated — a
    short-history symbol cannot be silently counted as 'below'."""
    closes_by_symbol = {
        "A": [1.0, 2.0, 3.0, 4.0, 5.0],     # SMA(3) = 4; 5 > 4 → above
        "B": [10.0, 10.0, 10.0, 10.0, 1.0],  # SMA(3) ≈ 7; 1 < 7 → below
        "C": [1.0, 2.0],                     # too short → excluded
    }
    cell = B.compute_breadth(closes_by_symbol=closes_by_symbol, window=3)
    assert cell.pct == 0.5
    assert cell.counted == 2
    assert cell.total == 3


def test_compute_breadth_returns_none_pct_when_nobody_qualifies():
    cell = B.compute_breadth(closes_by_symbol={"A": [1.0]}, window=10)
    assert cell.pct is None
    assert cell.counted == 0
    assert cell.total == 1


def test_max_window_matches_largest_timeframe():
    assert B.MAX_WINDOW == max(n for _, n in B.TIMEFRAMES)
