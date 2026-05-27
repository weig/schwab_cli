from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteRow:
    symbol: str
    last: float | None
    change: float | None
    change_pct: float | None
    bid: float | None
    ask: float | None
    volume: int | None  # Schwab `totalVolume` is an integer share count.
    error: str | None = None


@dataclass(frozen=True)
class QuoteResult:
    rows: tuple[QuoteRow, ...]
