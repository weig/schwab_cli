"""Ticker resolver — turn any reasonable user-facing symbol string into a
structured :class:`Ticker`, and emit the canonical OSI-padded form Schwab's
API expects.

Accepts all of these and resolves them identically::

    "NVDA"                          → stock
    "NVDA260501C240"                → option
    "NVDA  260501C240"              → option (Schwab-style padding)
    "NVDA260501C240.0"              → option (decimal strike)
    "NVDA260501C00240000"           → option (full OSI 8-digit strike)

Design choices:

* The dataclass field is ``strike`` (not ``price``) to avoid collision with
  the option's *market* price elsewhere in the codebase. The canonical
  exported structure (``Ticker.to_dict()``) mirrors the request:
  ``{"type": ..., "underlying": ..., "option": {"date", "type", "strike"}}``.
* Two-digit years map to ``20YY``. Schwab's option universe is entirely
  21st-century; if that ever changes we'll add an explicit cutoff.
* The OSI strike format is exactly 8 digits with an implied 3-decimal
  precision (``00240000`` = $240.000). We detect OSI by length==8 and
  no decimal point; everything else parses as a plain number.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


class TickerError(ValueError):
    """Raised when a ticker string cannot be parsed."""


@dataclass(frozen=True)
class OptionPart:
    date: str     # YYYYMMDD
    type: str     # "C" or "P"
    strike: float


@dataclass(frozen=True)
class Ticker:
    type: str                 # "stock" or "option"
    underlying: str
    option: OptionPart | None

    def to_dict(self) -> dict:
        """JSON-compatible representation."""
        return {
            "type": self.type,
            "underlying": self.underlying,
            "option": asdict(self.option) if self.option is not None else None,
        }

    def to_schwab_symbol(self) -> str:
        """Canonical OSI-ish symbol Schwab's API accepts.

        Stock: returns the underlying untouched.
        Option: ``{underlying:<6}{YY}{MM}{DD}{C|P}{strike*1000:08d}`` — the
        underlying left-padded with spaces to 6 chars.
        """
        if self.type == "stock" or self.option is None:
            return self.underlying
        u = self.underlying.ljust(6)                 # "NVDA" -> "NVDA  "
        yymmdd = self.option.date[2:]                # 20260501 -> 260501
        strike_osi = f"{int(round(self.option.strike * 1000)):08d}"
        return f"{u}{yymmdd}{self.option.type}{strike_osi}"


# Option ticker: underlying (alphanumeric, may embed digits for synthetic
# names like EXACT6), optional whitespace (Schwab's "NVDA  260501C240"
# padding), then a strict YYMMDD date, put/call letter, and strike.
# Lazy match on the underlying so the date anchor wins backtracking.
_OPTION_RE = re.compile(
    r"^(?P<under>[A-Z][A-Z0-9.]*?)\s*"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d+(?:\.\d+)?)$"
)

# Stock ticker: alpha only (optionally with a ".X" share-class suffix).
# Deliberately strict — rejects digit-bearing strings so `NVDA260501C240`
# cannot accidentally parse as a stock after the option regex misses.
_STOCK_RE = re.compile(r"^[A-Z]+(?:\.[A-Z]+)?$")


def resolve(raw: str) -> Ticker:
    """Parse a raw ticker string into a :class:`Ticker`."""
    if raw is None:
        raise TickerError("ticker is None")
    s = raw.strip().upper()
    if not s:
        raise TickerError("ticker is empty")

    # Always try option first: option inputs look like "<stock><digits>..."
    # which would also pass a too-loose stock regex.
    compact = re.sub(r"\s+", "", s)
    m = _OPTION_RE.match(compact)
    if m:
        date = f"20{m.group('yy')}{m.group('mm')}{m.group('dd')}"
        return Ticker(
            type="option",
            underlying=m.group("under"),
            option=OptionPart(
                date=date,
                type=m.group("cp"),
                strike=_parse_strike(m.group("strike")),
            ),
        )

    # Not an option — must be a pure-alpha stock ticker.
    if _STOCK_RE.match(compact):
        return Ticker(type="stock", underlying=compact, option=None)

    raise TickerError(f"unrecognized ticker format: {raw!r}")


def _parse_strike(s: str) -> float:
    """Turn a strike token into a float.

    * ``"240"`` → ``240.0``
    * ``"240.5"`` → ``240.5``
    * ``"00240000"`` (exactly 8 digits, no decimal) → ``240.0`` (OSI form)
    """
    if "." in s:
        return float(s)
    if len(s) == 8 and s.isdigit():
        return int(s) / 1000.0
    return float(int(s))
