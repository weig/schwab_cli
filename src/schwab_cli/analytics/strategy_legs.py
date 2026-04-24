"""Option-leg parser for the ``strategy`` command.

Grammar: ``±N@YYYYMMDD{C|P}STRIKE``

Each ``--leg`` token describes one contract in a multi-leg position:

* ``±N``     signed integer quantity. ``+`` (or omitted) = buy (long);
              ``-`` = sell (short). Zero is rejected.
* ``@``      required separator.
* ``YYYYMMDD`` full 8-digit calendar date; must be a real date.
* ``C`` / ``P`` side; lowercase accepted and normalised.
* ``STRIKE`` positive number (int or float).

Examples::

    -1@20260501P270.5    sell 1 put, 2026-05-01, strike 270.5
    +2@20260501C255      buy 2 calls, 2026-05-01, strike 255
    1@20260701P300       buy 1 put (unsigned = buy)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

Side = Literal["C", "P"]


class LegParseError(ValueError):
    """Raised when a ``--leg`` token cannot be parsed."""


@dataclass(frozen=True)
class Leg:
    """One option contract in a multi-leg position.

    ``qty`` is signed: positive = long, negative = short. The absolute
    value is the contract count (ratios like 2:1 use ``qty=2`` paired
    with ``qty=-1``).
    """

    qty: int
    side: Side
    expiry: date
    strike: float

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0


# Grammar: optional sign, digits, '@', 8 digits, side char, strike (int or float).
# Side and strike parsed loosely here so we can emit specific errors below.
_LEG_RE = re.compile(
    r"""
    ^
    (?P<sign>[+-]?)          # optional sign
    (?P<qty>\d+)             # magnitude
    @                        # separator
    (?P<date>\d{8})          # YYYYMMDD
    (?P<side>[A-Za-z])       # C or P (case-insensitive, validated later)
    (?P<strike>-?[\d.]+)     # strike, signed checked below so we can reject it
    $
    """,
    re.VERBOSE,
)


def parse_leg(token: str) -> Leg:
    """Parse one ``--leg`` token into a :class:`Leg`.

    Raises :class:`LegParseError` with a human-friendly message that
    names the specific failure (missing ``@``, bad date, bad side, bad
    strike, zero quantity, etc.).
    """
    if token is None:
        raise LegParseError("leg is empty")
    s = token.strip()
    if not s:
        raise LegParseError("leg is empty")

    # Missing quantity first — otherwise "@20260501C255" falls through to
    # the generic regex miss and the error message is unhelpful.
    if s.startswith("@"):
        raise LegParseError(f"{token!r}: missing quantity before '@'")

    # "@" is a grammar-level must-have — flag it explicitly so users see
    # what's wrong instead of a generic "bad grammar" blob.
    if "@" not in s:
        raise LegParseError(f"{token!r}: expected '@' between quantity and date")

    # Reject fractional or non-integer quantities up front; they'd slip
    # past the regex as e.g. "1.5@..." matching qty=1 then leaving ".5@..."
    # as the remainder, which would fail with a less helpful message.
    qty_part, _, rest = s.partition("@")
    if "." in qty_part:
        raise LegParseError(
            f"{token!r}: quantity must be an integer, got {qty_part!r}"
        )

    m = _LEG_RE.match(s)
    if not m:
        # Diagnose what's likely wrong so the error points at the spot.
        if not rest:
            raise LegParseError(f"{token!r}: empty leg body after '@'")
        # Peel the rest apart to find the first offending component.
        _diagnose_after_at(token, rest)
        raise LegParseError(f"{token!r}: invalid leg syntax")

    sign = m.group("sign") or "+"
    qty_mag = int(m.group("qty"))
    if qty_mag == 0:
        raise LegParseError(f"{token!r}: quantity cannot be zero")
    qty = qty_mag if sign == "+" else -qty_mag

    date_str = m.group("date")
    try:
        expiry = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        raise LegParseError(
            f"{token!r}: invalid date {date_str!r} (expected YYYYMMDD)"
        )

    side_raw = m.group("side").upper()
    if side_raw not in ("C", "P"):
        raise LegParseError(
            f"{token!r}: side must be C or P, got {m.group('side')!r}"
        )

    strike_str = m.group("strike")
    if strike_str.startswith("-"):
        raise LegParseError(f"{token!r}: strike must be positive, got {strike_str!r}")
    try:
        strike = float(strike_str)
    except ValueError:
        raise LegParseError(f"{token!r}: invalid strike {strike_str!r}")
    if strike <= 0:
        raise LegParseError(f"{token!r}: strike must be positive, got {strike}")

    return Leg(qty=qty, side=side_raw, expiry=expiry, strike=strike)


def _diagnose_after_at(token: str, rest: str) -> None:
    """Attempt to pinpoint which part after ``@`` is broken so the error
    message names it rather than blaming the whole token. Raises
    :class:`LegParseError` on the first specific failure it finds;
    returns silently if it can't distinguish, letting the caller emit a
    generic message."""
    if len(rest) < 8:
        raise LegParseError(
            f"{token!r}: date must be 8 digits YYYYMMDD, got {rest[:8]!r}"
        )

    date_str = rest[:8]
    if not date_str.isdigit():
        raise LegParseError(
            f"{token!r}: invalid date {date_str!r} (expected 8 digits YYYYMMDD)"
        )
    try:
        date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        raise LegParseError(f"{token!r}: invalid date {date_str!r}")

    if len(rest) < 9:
        raise LegParseError(f"{token!r}: missing side (C or P) after date")
    side_char = rest[8]
    if side_char.upper() not in ("C", "P"):
        raise LegParseError(
            f"{token!r}: side must be C or P, got {side_char!r}"
        )

    strike_str = rest[9:]
    if not strike_str:
        raise LegParseError(f"{token!r}: missing strike after side")
    try:
        v = float(strike_str)
    except ValueError:
        raise LegParseError(f"{token!r}: invalid strike {strike_str!r}")
    if v <= 0:
        raise LegParseError(f"{token!r}: strike must be positive, got {v}")
