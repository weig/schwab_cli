from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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


@dataclass(frozen=True)
class GreeksResult:
    """Single-contract greeks view.

    Wraps the display envelope dict that ``output.greeks.render_greeks``
    consumes verbatim, keeping rendered HUMAN/JSON/MD output byte-identical
    to the pre-migration command. The envelope shape is::

        {
            "underlyingSymbol": str,
            "expiry": str,            # ISO date
            "dte": int | None,
            "underlying": dict,       # {last, netChange, pctChange}
            "contract": dict,         # shaped option contract
        }

    Typed as a read-only ``Mapping`` to signal callers must not mutate it.
    """

    envelope: Mapping[str, Any]
