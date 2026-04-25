"""Schwab / TOS-style natural-language order ticket parser.

Lets the user paste a Schwab confirmation-page line as one argument
to ``schwab_cli order place --parse "..."`` instead of building the
order with individual flags.

Phase 1 grammar covers:

* single-leg option orders, e.g.
  ``BUY +2 AAPL 100 17 JAN 26 250 CALL @1.20 LMT``
* equity orders, e.g.
  ``BUY +100 NVDA @150.00 LMT DAY``
  ``SELL -50 TSLA MKT``
* ``VERTICAL`` spreads with two strikes ``LOWER/HIGHER``, e.g.
  ``BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 262.5/267.5 CALL @2.35 LMT``

Other multi-leg strategies (``CALENDAR``, ``DIAGONAL``, ``BUTTERFLY``,
``CONDOR``, ``IRON CONDOR``, ``STRADDLE``, ``STRANGLE``, ``COVERED``,
``CUSTOM``) are deferred to Phase 2.

OSI option symbols (the 21-character format Schwab requires on each
option leg's ``instrument.symbol``) are produced by :func:`to_osi`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal


class TicketParseError(ValueError):
    """Raised when the ``--parse`` argument can't be parsed."""

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


Side = Literal["BUY", "SELL"]
OptionType = Literal["CALL", "PUT"]
OrderType = Literal["LIMIT", "MARKET", "NET_DEBIT", "NET_CREDIT"]
Duration = Literal["DAY", "GOOD_TILL_CANCEL"]


@dataclass(frozen=True)
class ParsedLeg:
    """One option contract derived from a parsed ticket."""

    instruction: str          # BUY_TO_OPEN / SELL_TO_OPEN / BUY_TO_CLOSE / SELL_TO_CLOSE
    quantity: int             # always positive
    underlying: str           # e.g. "AMZN"
    expiry: date
    option_type: OptionType
    strike: float


@dataclass(frozen=True)
class ParsedTicket:
    """Result of parsing a Schwab/TOS-style order ticket.

    For equity orders ``option_type``/``strikes``/``expiry`` are
    ``None`` and ``legs`` is empty (the equity is described by
    ``side``, ``quantity``, ``underlying``).
    """

    side: Side
    quantity: int                          # spread count or share count; absolute value
    underlying: str
    order_type: OrderType
    duration: Duration
    price: float | None                    # None for MARKET
    strategy: str | None                   # "VERTICAL" or None
    expiry: date | None                    # None for equity
    option_type: OptionType | None         # None for equity
    strikes: tuple[float, ...] = field(default_factory=tuple)
    legs: tuple[ParsedLeg, ...] = field(default_factory=tuple)

    @property
    def is_option(self) -> bool:
        return bool(self.legs)

    @property
    def is_equity(self) -> bool:
        return not self.legs


_MONTHS: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_PHASE2_STRATEGIES = {
    "CALENDAR", "DIAGONAL", "BUTTERFLY", "CONDOR", "IRON",  # IRON CONDOR
    "STRADDLE", "STRANGLE", "COVERED", "CUSTOM",
    "BACK_RATIO", "STRADDLE/STRANGLE",
}


def parse_ticket(s: str, *, today: date | None = None) -> ParsedTicket:
    """Parse a Schwab/TOS ticket string into a :class:`ParsedTicket`.

    ``today`` is injectable for deterministic tests; otherwise the
    parser uses :meth:`date.today` only when expanding the 2-digit
    year (which it does as ``2000 + YY``, so ``today`` is currently
    unused — kept as a parameter so we can switch to a sliding
    window later without an API change).
    """
    if not s or not s.strip():
        raise TicketParseError("ticket is empty")

    # Normalise: collapse whitespace, drop the leading "+" sometimes
    # present on share counts, but preserve the QTY sign on the second
    # token.
    raw = s.strip()
    # Strip out parenthetical decorations like "(Weeklys)" or "(Monthly)"
    # — they're informational on TOS strings and don't affect the body.
    raw = re.sub(r"\([^)]*\)", " ", raw)
    tokens = raw.split()
    if len(tokens) < 4:
        raise TicketParseError(
            f"ticket too short, expected at minimum SIDE QTY SYMBOL ORDER_TYPE: {s!r}"
        )

    cursor = 0

    # ---- SIDE ----
    side_tok = tokens[cursor].upper()
    if side_tok not in ("BUY", "SELL"):
        raise TicketParseError(
            f"first token must be BUY or SELL, got {tokens[cursor]!r}"
        )
    side: Side = side_tok  # type: ignore[assignment]
    cursor += 1

    # ---- QTY (signed) ----
    qty_tok = tokens[cursor]
    qty_match = re.fullmatch(r"[+-]?\d+", qty_tok)
    if not qty_match:
        raise TicketParseError(
            f"expected signed quantity after SIDE, got {qty_tok!r}"
        )
    quantity = abs(int(qty_tok))
    if quantity == 0:
        raise TicketParseError("quantity cannot be zero")
    cursor += 1

    # ---- optional STRATEGY keyword ----
    strategy: str | None = None
    next_tok = tokens[cursor].upper()
    if next_tok == "VERTICAL":
        strategy = "VERTICAL"
        cursor += 1
    elif next_tok in _PHASE2_STRATEGIES:
        raise TicketParseError(
            f"strategy {next_tok!r} not supported in Phase 1; "
            "use --leg or wait for Phase 2",
            kind="phase2",
        )

    # ---- UNDERLYING ----
    if cursor >= len(tokens):
        raise TicketParseError(f"missing underlying symbol: {s!r}")
    underlying = tokens[cursor].upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9./]*", underlying):
        raise TicketParseError(
            f"underlying must look like a ticker, got {tokens[cursor]!r}"
        )
    cursor += 1

    # ---- optional MULTIPLIER (literal "100") — informational ----
    if cursor < len(tokens) and tokens[cursor] == "100":
        cursor += 1

    # ---- branch: equity vs option ----
    # Option ticket continues with: D MON YY STRIKES C/P @PX TYPE [DUR]
    # Equity ticket continues with: [@PX] TYPE [DUR]
    looks_like_option = (
        cursor + 4 < len(tokens)
        and re.fullmatch(r"\d{1,2}", tokens[cursor])
        and tokens[cursor + 1].upper() in _MONTHS
    )

    if looks_like_option:
        return _finish_option(
            s, side, quantity, strategy, underlying, tokens, cursor,
        )
    return _finish_equity(s, side, quantity, underlying, tokens, cursor, strategy)


def _finish_option(
    raw: str,
    side: Side,
    quantity: int,
    strategy: str | None,
    underlying: str,
    tokens: list[str],
    cursor: int,
) -> ParsedTicket:
    # ---- EXPIRY: "D MON YY" ----
    if cursor + 2 >= len(tokens):
        raise TicketParseError(
            f"expected expiry as 'D MON YY' (e.g. '1 MAY 26'): {raw!r}"
        )
    day_tok = tokens[cursor]
    mon_tok = tokens[cursor + 1].upper()
    yr_tok = tokens[cursor + 2]
    try:
        day_num = int(day_tok)
    except ValueError:
        raise TicketParseError(f"expiry day must be a number, got {day_tok!r}")
    if mon_tok not in _MONTHS:
        raise TicketParseError(
            f"expiry month must be JAN..DEC, got {tokens[cursor + 1]!r}"
        )
    try:
        yr_num = int(yr_tok)
    except ValueError:
        raise TicketParseError(f"expiry year must be a number, got {yr_tok!r}")
    if yr_num < 100:
        yr_num = 2000 + yr_num
    try:
        expiry = date(yr_num, _MONTHS[mon_tok], day_num)
    except ValueError as e:
        raise TicketParseError(
            f"invalid expiry date {day_tok} {mon_tok} {yr_tok}: {e}"
        )
    cursor += 3

    # ---- STRIKES ----
    if cursor >= len(tokens):
        raise TicketParseError(f"expected strike(s) after expiry: {raw!r}")
    strikes_tok = tokens[cursor]
    cursor += 1
    if "/" in strikes_tok:
        if strategy is None:
            raise TicketParseError(
                f"multi-strike ticket requires a strategy keyword "
                f"(e.g. VERTICAL) before the underlying, got {strikes_tok!r}"
            )
        if strategy == "VERTICAL":
            parts = strikes_tok.split("/")
            if len(parts) != 2:
                raise TicketParseError(
                    f"VERTICAL requires two strikes separated by '/' "
                    f"(got {strikes_tok!r})"
                )
            try:
                strikes = tuple(float(x) for x in parts)
            except ValueError:
                raise TicketParseError(f"invalid strike(s) {strikes_tok!r}")
            if any(v <= 0 for v in strikes):
                raise TicketParseError(
                    f"strikes must be positive, got {strikes_tok!r}"
                )
            if strikes[0] == strikes[1]:
                raise TicketParseError(
                    f"VERTICAL strikes must differ, got {strikes_tok!r}"
                )
        else:
            raise TicketParseError(
                f"strategy {strategy!r} multi-strike layout not supported in "
                "Phase 1",
                kind="phase2",
            )
    else:
        if strategy is not None:
            raise TicketParseError(
                f"strategy {strategy!r} requires multiple strikes separated "
                f"by '/', got single strike {strikes_tok!r}"
            )
        try:
            strikes = (float(strikes_tok),)
        except ValueError:
            raise TicketParseError(f"invalid strike {strikes_tok!r}")
        if strikes[0] <= 0:
            raise TicketParseError(f"strike must be positive, got {strikes_tok!r}")

    # ---- OPTION_TYPE ----
    if cursor >= len(tokens):
        raise TicketParseError(f"expected CALL or PUT after strikes: {raw!r}")
    type_tok = tokens[cursor].upper()
    if type_tok not in ("CALL", "PUT"):
        raise TicketParseError(
            f"option type must be CALL or PUT, got {tokens[cursor]!r}"
        )
    option_type: OptionType = type_tok  # type: ignore[assignment]
    cursor += 1

    # ---- @PRICE + ORDER_TYPE [+ DURATION] ----
    price, order_type, duration = _parse_price_type_duration(
        tokens, cursor, raw, default_when_combo=("LIMIT" if strategy is None else "NET"),
    )

    # For VERTICAL: BUY → NET_DEBIT, SELL → NET_CREDIT (for LMT). MKT → MARKET.
    if strategy == "VERTICAL":
        if order_type == "LIMIT":
            order_type = "NET_DEBIT" if side == "BUY" else "NET_CREDIT"
        # MARKET stays MARKET (rare but allowed by Schwab for spreads via NET_ZERO,
        # but Phase 1 keeps MARKET single-instruction; a spread MKT is uncommon
        # enough we punt on encoding it).

    # ---- expand legs ----
    legs = _expand_legs(side, quantity, strategy, underlying, expiry, option_type, strikes)

    return ParsedTicket(
        side=side,
        quantity=quantity,
        underlying=underlying,
        order_type=order_type,
        duration=duration,
        price=price,
        strategy=strategy,
        expiry=expiry,
        option_type=option_type,
        strikes=strikes,
        legs=legs,
    )


def _finish_equity(
    raw: str,
    side: Side,
    quantity: int,
    underlying: str,
    tokens: list[str],
    cursor: int,
    strategy: str | None,
) -> ParsedTicket:
    if strategy is not None:
        raise TicketParseError(
            f"strategy {strategy!r} requires an option ticket "
            f"(missing expiry / strike / CALL or PUT): {raw!r}"
        )
    price, order_type, duration = _parse_price_type_duration(
        tokens, cursor, raw, default_when_combo="LIMIT",
    )
    return ParsedTicket(
        side=side,
        quantity=quantity,
        underlying=underlying,
        order_type=order_type,
        duration=duration,
        price=price,
        strategy=None,
        expiry=None,
        option_type=None,
        strikes=(),
        legs=(),
    )


def _parse_price_type_duration(
    tokens: list[str],
    cursor: int,
    raw: str,
    *,
    default_when_combo: str,
) -> tuple[float | None, OrderType, Duration]:
    """Parse trailing ``[@PRICE] ORDER_TYPE [DURATION]``.

    Returns ``(price, order_type, duration)``. ``price`` is ``None``
    for MARKET orders.
    """
    if cursor >= len(tokens):
        raise TicketParseError(
            f"expected order type (LMT or MKT) at end of ticket: {raw!r}"
        )

    price: float | None = None
    if tokens[cursor].startswith("@"):
        price_tok = tokens[cursor][1:]
        try:
            price = float(price_tok)
        except ValueError:
            raise TicketParseError(f"invalid @price {tokens[cursor]!r}")
        if price <= 0:
            raise TicketParseError(
                f"price must be positive, got {tokens[cursor]!r}"
            )
        cursor += 1

    if cursor >= len(tokens):
        raise TicketParseError(
            f"expected order type (LMT or MKT) after price: {raw!r}"
        )
    type_tok = tokens[cursor].upper()
    cursor += 1
    if type_tok == "LMT":
        if price is None:
            raise TicketParseError("LMT requires @<price>")
        order_type: OrderType = "LIMIT"  # caller may rewrite to NET_DEBIT/NET_CREDIT
    elif type_tok == "MKT":
        if price is not None:
            raise TicketParseError("MKT must not have @<price>")
        order_type = "MARKET"
    else:
        raise TicketParseError(
            f"order type must be LMT or MKT (Phase 1), got {type_tok!r}"
        )

    duration: Duration = "DAY"
    if cursor < len(tokens):
        dur_tok = tokens[cursor].upper()
        if dur_tok == "DAY":
            duration = "DAY"
        elif dur_tok in ("GTC", "GOOD_TILL_CANCEL", "GOOD_TIL_CANCEL"):
            duration = "GOOD_TILL_CANCEL"
        else:
            raise TicketParseError(
                f"duration must be DAY or GTC, got {tokens[cursor]!r}"
            )
        cursor += 1

    if cursor < len(tokens):
        leftover = " ".join(tokens[cursor:])
        raise TicketParseError(
            f"unexpected trailing tokens after order type: {leftover!r}"
        )

    return price, order_type, duration


def _expand_legs(
    side: Side,
    quantity: int,
    strategy: str | None,
    underlying: str,
    expiry: date,
    option_type: OptionType,
    strikes: tuple[float, ...],
) -> tuple[ParsedLeg, ...]:
    """Turn the strategy + side + strikes into concrete leg instructions."""
    if strategy is None:
        # Single leg: BUY → BUY_TO_OPEN, SELL → SELL_TO_OPEN.
        # (Phase 4 may add closing semantics via an explicit suffix.)
        instruction = f"{side}_TO_OPEN"
        return (
            ParsedLeg(
                instruction=instruction,
                quantity=quantity,
                underlying=underlying,
                expiry=expiry,
                option_type=option_type,
                strike=strikes[0],
            ),
        )

    if strategy == "VERTICAL":
        lower, higher = strikes
        if option_type == "CALL":
            # BUY: BTO lower, STO higher. SELL inverts.
            if side == "BUY":
                instr_lower, instr_higher = "BUY_TO_OPEN", "SELL_TO_OPEN"
            else:
                instr_lower, instr_higher = "SELL_TO_OPEN", "BUY_TO_OPEN"
        else:  # PUT
            if side == "BUY":
                instr_lower, instr_higher = "SELL_TO_OPEN", "BUY_TO_OPEN"
            else:
                instr_lower, instr_higher = "BUY_TO_OPEN", "SELL_TO_OPEN"
        return (
            ParsedLeg(
                instruction=instr_lower, quantity=quantity,
                underlying=underlying, expiry=expiry,
                option_type=option_type, strike=lower,
            ),
            ParsedLeg(
                instruction=instr_higher, quantity=quantity,
                underlying=underlying, expiry=expiry,
                option_type=option_type, strike=higher,
            ),
        )

    raise TicketParseError(
        f"internal: unhandled strategy {strategy!r}", kind="phase2",
    )


# ---- OSI symbol -----------------------------------------------------------


def to_osi(
    underlying: str, expiry: date, option_type: OptionType, strike: float,
) -> str:
    """Build the 21-character OSI option symbol Schwab expects.

    Layout: ``[UND left-justified to 6][YYMMDD][C|P][strike*1000 zero-padded to 8]``.

    Examples::

        to_osi("NVDA", date(2026, 1, 17), "CALL", 250)
            → "NVDA  260117C00250000"
        to_osi("AMZN", date(2026, 5, 1), "PUT", 262.5)
            → "AMZN  260501P00262500"

    Raises :class:`ValueError` if ``underlying`` exceeds 6 characters
    or ``strike`` is non-positive / out of range for an 8-digit
    integer (strike × 1000 < 100 000 000).
    """
    und = underlying.upper().strip()
    if not und or len(und) > 6:
        raise ValueError(f"underlying must be 1-6 chars, got {underlying!r}")
    if option_type not in ("CALL", "PUT"):
        raise ValueError(f"option_type must be CALL or PUT, got {option_type!r}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")

    # Schwab requires the strike encoded as an 8-digit integer of
    # (strike * 1000). Round to the nearest cent first to avoid float
    # representation surprises (e.g. 262.5 → 262500.0000001).
    strike_int = round(strike * 1000)
    if strike_int >= 10**8:
        raise ValueError(
            f"strike {strike} too large for 8-digit OSI encoding"
        )

    side = "C" if option_type == "CALL" else "P"
    yymmdd = expiry.strftime("%y%m%d")
    return f"{und:<6}{yymmdd}{side}{strike_int:08d}"
