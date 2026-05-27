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


@dataclass(frozen=True)
class VolResult:
    """Volatility-context view (IV / HV / HVP / IVP / P/C).

    Wraps the display envelope dict that ``output.vol.render_vol`` consumes
    verbatim, keeping rendered HUMAN/JSON/MD output byte-identical to the
    pre-migration command. The envelope shape is::

        {
            "symbol": str,
            "spot": float,
            "iv": dict,
            "iv_ref": dict | None,
            "hv": dict,
            "hvp": dict,
            "pc": dict,
            "ivp": dict,
            "ivr_ivp": dict,
        }

    ``storage_error`` carries a non-fatal SQLite error string (or ``None``)
    so the command can surface the "IVP may be stale" warning while still
    rendering. Typed as a read-only ``Mapping`` to signal callers must not
    mutate the envelope.
    """

    envelope: Mapping[str, Any]
    storage_error: str | None = None


@dataclass(frozen=True)
class AccountsResult:
    """List-of-accounts view.

    Wraps the raw Schwab account payloads (one ``Mapping`` per account)
    that ``output.accounts.render_accounts`` consumes verbatim, keeping the
    rendered HUMAN/JSON/MD output byte-identical to the pre-migration
    command. Typed as a read-only ``tuple`` of ``Mapping`` to signal callers
    must not mutate it.
    """

    accounts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class AccountResult:
    """Single-account view.

    Wraps the raw Schwab account payload that
    ``output.accounts.render_account`` consumes verbatim. Typed as a
    read-only ``Mapping`` to signal callers must not mutate it.
    """

    account: Mapping[str, Any]


@dataclass(frozen=True)
class PositionsResult:
    """Flat list of position rows view.

    Wraps the raw position-row dicts (each carrying the synthetic
    ``_account`` key) that ``output.accounts.render_positions`` consumes
    verbatim. Typed as a read-only ``tuple`` of ``Mapping`` to signal
    callers must not mutate it.
    """

    positions: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class HistoryResult:
    """OHLCV price-history view.

    Wraps the display envelope dict that ``output.history.render_history``
    consumes verbatim, keeping rendered HUMAN/JSON/MD output byte-identical
    to the pre-migration command. The envelope shape is::

        {
            "symbol": str,
            "interval": str,
            "from": str | None,        # ISO NY datetime
            "to": str | None,          # ISO NY datetime
            "previousClose": float | None,
            "candles": list[dict],     # shaped OHLCV rows
        }

    Typed as a read-only ``Mapping`` to signal callers must not mutate it.
    """

    envelope: Mapping[str, Any]
