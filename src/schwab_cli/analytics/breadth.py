"""Market breadth: percentage of index constituents trading above
their N-period simple moving average.

Pure-Python — no numpy. Inputs are lists of closes (oldest→newest) per
symbol; output is a per-(index, timeframe) pct + valid-symbol count.
"""
from __future__ import annotations

from dataclasses import dataclass


# Bloomberg-style timeframes. Trading-day approximations:
#   48W ≈ 240d, 96W ≈ 480d, 1Y ≈ 252d, 2Y ≈ 504d.
TIMEFRAMES: list[tuple[str, int]] = [
    ("5D",   5),
    ("10D",  10),
    ("30D",  30),
    ("60D",  60),
    ("90D",  90),
    ("180D", 180),
    ("48W",  240),
    ("96W",  480),
    ("1Y",   252),
    ("2Y",   504),
]

MAX_WINDOW: int = max(n for _, n in TIMEFRAMES)


@dataclass(frozen=True)
class BreadthCell:
    """One (index × timeframe) cell — pct above MA + how many symbols
    had enough history to be counted."""
    pct: float | None
    counted: int
    total: int


def above_ma(closes: list[float], window: int) -> bool | None:
    """True when the last close > SMA(window). Returns ``None`` if
    there aren't at least ``window`` historical closes to form the
    SMA — caller treats None as 'not enough data, exclude from pct'.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    if len(closes) < window:
        return None
    sma = sum(closes[-window:]) / window
    return closes[-1] > sma


def compute_breadth(
    *,
    closes_by_symbol: dict[str, list[float]],
    window: int,
) -> BreadthCell:
    """Aggregate ``above_ma`` over a group of symbols.

    Symbols with insufficient history are excluded from the numerator
    AND the denominator — the user sees coverage via ``counted`` so
    a thin sample (e.g. early SPX in 2Y window) isn't silently averaged
    against the full member count.
    """
    above = 0
    counted = 0
    for closes in closes_by_symbol.values():
        verdict = above_ma(closes, window)
        if verdict is None:
            continue
        counted += 1
        if verdict:
            above += 1
    pct = (above / counted) if counted else None
    return BreadthCell(pct=pct, counted=counted, total=len(closes_by_symbol))
