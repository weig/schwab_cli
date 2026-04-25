"""Option-leg parser for the ``strategy`` and ``order`` commands.

Grammar: ``±N@YYMMDD{C|P}STRIKE[o|c]`` (preferred; OSI-aligned)
         ``±N@YYYYMMDD{C|P}STRIKE[o|c]`` (also accepted, legacy)

Each ``--leg`` token describes one contract in a multi-leg position:

* ``±N``     signed integer quantity. ``+`` (or omitted) = buy (long);
              ``-`` = sell (short). Zero is rejected.
* ``@``      required separator.
* date       6 digits (YYMMDD, e.g. ``260501``) — matches OSI symbols
              and the ``chain`` command. 8 digits (YYYYMMDD) is also
              accepted for backward compatibility.
* ``C`` / ``P`` side; lowercase accepted and normalised.
* ``STRIKE`` positive number (int or float).
* ``o`` / ``c`` optional **open/close suffix**: ``o`` (default, opening)
              or ``c`` (closing). Combined with the sign this maps to
              the Schwab ``instruction`` field — see :data:`Effect`.

Examples::

    -1@260501P270.5     sell to open 1 put, 2026-05-01, strike 270.5
    +2@260501C255       buy to open 2 calls
    1@260701P300        buy to open 1 put (unsigned = buy)
    +1@260117C250c      buy to CLOSE 1 call (closing a short)
    -1@20260117P200c    legacy YYYYMMDD form; equivalent to 260117P200c
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

Side = Literal["C", "P"]
Effect = Literal["o", "c"]
Instruction = Literal[
    "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"
]


class LegParseError(ValueError):
    """Raised when a ``--leg`` token cannot be parsed."""


@dataclass(frozen=True)
class Leg:
    """One option contract in a multi-leg position.

    ``qty`` is signed: positive = long, negative = short. The absolute
    value is the contract count (ratios like 2:1 use ``qty=2`` paired
    with ``qty=-1``).

    ``effect`` is the open/close intent (``"o"`` opening, ``"c"`` closing),
    needed to derive the Schwab ``instruction`` field for orders. Defaults
    to ``"o"`` since opening is the dominant case and analytics-only
    callers (the existing strategy command) don't care.
    """

    qty: int
    side: Side
    expiry: date
    strike: float
    effect: Effect = "o"

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def instruction(self) -> Instruction:
        """Schwab ``instruction`` derived from sign + open/close effect."""
        if self.qty > 0:
            return "BUY_TO_OPEN" if self.effect == "o" else "BUY_TO_CLOSE"
        return "SELL_TO_OPEN" if self.effect == "o" else "SELL_TO_CLOSE"


# Grammar: optional sign, digits, '@', 6 or 8 digits (YYMMDD or YYYYMMDD),
# side char, strike (int or float), optional open/close suffix. Side,
# strike, and suffix parsed loosely here so we can emit specific errors
# below.
_LEG_RE = re.compile(
    r"""
    ^
    (?P<sign>[+-]?)          # optional sign
    (?P<qty>\d+)             # magnitude
    @                        # separator
    (?P<date>\d{6}|\d{8})    # YYMMDD (preferred) or YYYYMMDD (legacy)
    (?P<side>[A-Za-z])       # C or P (case-insensitive, validated later)
    (?P<strike>-?[\d.]+?)    # strike, signed checked below so we can reject it
    (?P<effect>[A-Za-z]?)    # optional open/close suffix
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
        if len(date_str) == 6:
            # YYMMDD — assume 21st century (2000 + YY).
            expiry = date(
                2000 + int(date_str[:2]),
                int(date_str[2:4]),
                int(date_str[4:6]),
            )
        else:
            expiry = date(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
            )
    except ValueError:
        raise LegParseError(
            f"{token!r}: invalid date {date_str!r} "
            f"(expected YYMMDD or YYYYMMDD)"
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

    effect_raw = m.group("effect").lower()
    if effect_raw == "":
        effect: Effect = "o"
    elif effect_raw in ("o", "c"):
        effect = effect_raw  # type: ignore[assignment]
    else:
        raise LegParseError(
            f"{token!r}: open/close suffix must be 'o' or 'c', got {m.group('effect')!r}"
        )

    return Leg(qty=qty, side=side_raw, expiry=expiry, strike=strike, effect=effect)


def _diagnose_after_at(token: str, rest: str) -> None:
    """Attempt to pinpoint which part after ``@`` is broken so the error
    message names it rather than blaming the whole token. Raises
    :class:`LegParseError` on the first specific failure it finds;
    returns silently if it can't distinguish, letting the caller emit a
    generic message."""
    # Pick the longest leading digit run as the candidate date — handles
    # both YYMMDD (6) and YYYYMMDD (8). Anything outside [6, 8] is bad.
    digit_run = 0
    for ch in rest:
        if ch.isdigit():
            digit_run += 1
        else:
            break
    if digit_run not in (6, 8):
        raise LegParseError(
            f"{token!r}: date must be 6 digits (YYMMDD) or 8 digits "
            f"(YYYYMMDD), got {rest[:max(digit_run, 1)]!r}"
        )

    date_str = rest[:digit_run]
    try:
        if digit_run == 6:
            date(2000 + int(date_str[:2]),
                 int(date_str[2:4]),
                 int(date_str[4:6]))
        else:
            date(int(date_str[:4]),
                 int(date_str[4:6]),
                 int(date_str[6:8]))
    except ValueError:
        raise LegParseError(f"{token!r}: invalid date {date_str!r}")

    if len(rest) <= digit_run:
        raise LegParseError(f"{token!r}: missing side (C or P) after date")
    side_char = rest[digit_run]
    if side_char.upper() not in ("C", "P"):
        raise LegParseError(
            f"{token!r}: side must be C or P, got {side_char!r}"
        )

    strike_str = rest[digit_run + 1:]
    if not strike_str:
        raise LegParseError(f"{token!r}: missing strike after side")
    try:
        v = float(strike_str)
    except ValueError:
        raise LegParseError(f"{token!r}: invalid strike {strike_str!r}")
    if v <= 0:
        raise LegParseError(f"{token!r}: strike must be positive, got {v}")
