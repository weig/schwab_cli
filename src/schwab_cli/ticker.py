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


def to_schwab_form(symbol: str) -> str:
    """Normalize a stock symbol to the form Schwab's API accepts.

    Schwab's quote / chain / history endpoints want class shares with
    a slash separator (``BRK/B``). Common alternative forms (``BRK.B``,
    ``BRK-B``) silently return empty results. This helper rewrites
    those into Schwab's form so the API layer can be agnostic about
    which convention the caller used.

    Symbols are also uppercased so renderer-side ``payload.get(symbol)``
    lookups match the keys Schwab returns (Schwab always replies with
    uppercase symbols regardless of the request casing).

    Idempotent — already-correct ``BRK/B`` and plain ``NVDA`` pass
    through unchanged. Option OSI strings (with embedded YYMMDD) are
    detected by the presence of a digit and uppercased — they already
    are by convention, but this stays safe for casual inputs.
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if any(c.isdigit() for c in s):
        # OSI option symbol (NVDA  260501C00240000).
        return s
    # Replace the *first* dot or dash with a slash. Avoid touching
    # subsequent characters in case some weird future ticker has
    # multiple separators.
    if "." in s:
        return s.replace(".", "/", 1)
    if "-" in s:
        return s.replace("-", "/", 1)
    return s


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
# Class-share separators: ``.`` (BRK.B), ``/`` (BRK/B), ``-`` (BRK-B).
# All three conventions show up in the wild; we accept any and
# canonicalize to the slash form Schwab's quote/history APIs expect.
_STOCK_RE = re.compile(r"^[A-Z]+(?:[./\-][A-Z]+)?$")

# Cash-settled index underlyings — Schwab requires the ``$`` prefix on the
# quote / chain / history endpoints (``$SPX``, ``$XSP``, ``$NDX``, ``$RUT``,
# ``$VIX``). Empirically ``SPX`` without the ``$`` 400s. We accept and
# PRESERVE the ``$`` (treated as a stock-type underlying so every
# underlying-handling code path works unchanged).
_INDEX_RE = re.compile(r"^\$[A-Z][A-Z0-9]*$")


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

    # Cash-settled index underlying (``$SPX`` / ``$XSP`` / ``$NDX`` / ``$RUT``
    # / ``$VIX``). Pass through with the ``$`` preserved — that is exactly
    # what Schwab's endpoints want.
    if _INDEX_RE.match(compact):
        return Ticker(type="stock", underlying=compact, option=None)

    # Not an option — must be a pure-alpha stock ticker.
    if _STOCK_RE.match(compact):
        # Canonicalize class-share separator to Schwab's slash form.
        # ``BRK.B`` / ``BRK-B`` → ``BRK/B`` so the underlying string
        # is what Schwab's quote/history endpoints expect.
        canonical = re.sub(r"[.\-]", "/", compact, count=1) if any(
            sep in compact for sep in ".-"
        ) else compact
        return Ticker(type="stock", underlying=canonical, option=None)

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
