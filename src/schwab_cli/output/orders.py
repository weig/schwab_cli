"""Output rendering for the ``order`` subcommand.

Renderers are pure: they take parsed data and return strings. No
network, no Schwab calls. Confirmation-panel data comes from upstream
callers in ``commands/order.py``.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table


_ET = ZoneInfo("America/New_York")


# ---- analytics for the confirmation panel ---------------------------------


@dataclass(frozen=True)
class OrderAnalytics:
    """Local-only analytics for the confirmation panel.

    Probability-of-profit is populated when the chain payload was
    fetched and we could anchor IV on the order's option legs;
    otherwise ``pop`` is ``None`` and the renderer falls back to a
    "(unavailable)" line.
    """

    max_profit: float | None    # dollars; None = unlimited
    max_loss: float | None      # dollars (negative); None = unlimited downside
    breakevens: tuple[float, ...]
    order_cost: float           # dollars; positive = debit, negative = credit
    pop: float | None = None    # probability of profit, 0..1


def compute_analytics(
    *,
    strategy: str | None,
    side: str,           # BUY / SELL
    option_type: str | None,  # CALL / PUT (None for equity)
    strikes: tuple[float, ...],
    quantity: int,
    price: float | None,
    body: dict | None = None,
    chain_data: dict | None = None,
) -> OrderAnalytics | None:
    """Return :class:`OrderAnalytics` for option orders we know how to
    score, or ``None`` for equity / unsupported shapes.

    ``price`` is the limit price per share (single leg) or per spread
    (vertical). For market orders we can't compute these → return ``None``.

    ``body`` + ``chain_data`` are optional. When both are supplied, the
    returned analytics carry a non-``None`` :attr:`OrderAnalytics.pop`
    computed against the chain's IV. Failure to compute POP is silent —
    ``pop`` falls back to ``None`` and the renderer prints "(unavailable)".
    """
    if not option_type or price is None:
        return None
    qty_mult = 100 * quantity

    def _with_pop(a: OrderAnalytics) -> OrderAnalytics:
        """Best-effort POP enrichment. Silent on any failure — the
        renderer treats ``pop=None`` as "(unavailable)"."""
        if not body or not chain_data:
            return a
        try:
            pop = _compute_pop(body, chain_data)
        except Exception:  # noqa: BLE001 — never block analytics on POP
            pop = None
        from dataclasses import replace
        return replace(a, pop=pop)

    if strategy is None:
        # Single-leg option.
        K = strikes[0]
        if side == "BUY":  # long
            cost = price * qty_mult
            if option_type == "CALL":
                return _with_pop(OrderAnalytics(
                    max_profit=None,                # unlimited upside
                    max_loss=-cost,
                    breakevens=(K + price,),
                    order_cost=cost,
                ))
            else:  # PUT
                return _with_pop(OrderAnalytics(
                    max_profit=(K - price) * qty_mult,
                    max_loss=-cost,
                    breakevens=(K - price,),
                    order_cost=cost,
                ))
        else:  # SELL
            credit = price * qty_mult
            if option_type == "CALL":
                return _with_pop(OrderAnalytics(
                    max_profit=credit,
                    max_loss=None,                  # unlimited
                    breakevens=(K + price,),
                    order_cost=-credit,
                ))
            else:  # PUT
                return _with_pop(OrderAnalytics(
                    max_profit=credit,
                    max_loss=-(K - price) * qty_mult,
                    breakevens=(K - price,),
                    order_cost=-credit,
                ))

    if strategy == "VERTICAL":
        lower, higher = strikes
        width = higher - lower
        if side == "BUY":
            # Debit spread.
            debit_per = price                       # per spread, per share
            max_loss = -debit_per * qty_mult
            max_profit = (width - debit_per) * qty_mult
            if option_type == "CALL":
                breakeven = lower + debit_per
            else:                                    # PUT
                breakeven = higher - debit_per
            return _with_pop(OrderAnalytics(
                max_profit=max_profit,
                max_loss=max_loss,
                breakevens=(breakeven,),
                order_cost=debit_per * qty_mult,
            ))
        else:  # SELL — credit spread
            credit_per = price
            max_profit = credit_per * qty_mult
            max_loss = -(width - credit_per) * qty_mult
            if option_type == "CALL":
                breakeven = lower + credit_per
            else:
                breakeven = higher - credit_per
            return _with_pop(OrderAnalytics(
                max_profit=max_profit,
                max_loss=max_loss,
                breakevens=(breakeven,),
                order_cost=-credit_per * qty_mult,
            ))

    return None


def _compute_pop(body: dict, chain_envelope: dict) -> float | None:
    """Probability of profit for ``body``'s option legs, against the
    flattened chain envelope.

    Builds :class:`PricedLeg` objects by matching each body leg's OSI
    symbol to a chain contract. Returns ``None`` when:

    * Spot is missing from the chain envelope.
    * No legs match a chain contract (e.g. unfetched expiry).
    * Every leg's IV resolves to zero (the underlying ``pop`` function
      then returns a deterministic 0/1 — useless as a probability).
    """
    from datetime import date as _date

    from schwab_cli.analytics.strategy import PricedLeg, pop as _pop_fn

    legs_raw = body.get("orderLegCollection") or []
    if not legs_raw:
        return None
    underlying = (chain_envelope.get("underlying") or {})
    spot = underlying.get("last")
    if not isinstance(spot, (int, float)) or spot <= 0:
        return None

    contracts = chain_envelope.get("contracts") or []
    # Index by (side, strike) for O(1) lookup.
    by_key: dict[tuple[str, float], dict] = {}
    for c in contracts:
        side_c = c.get("side")
        k = c.get("strike")
        if side_c in ("C", "P") and isinstance(k, (int, float)):
            by_key[(side_c, float(k))] = c

    priced: list[PricedLeg] = []
    dte: int | None = chain_envelope.get("dte")
    for leg in legs_raw:
        instr = (leg.get("instrument") or {})
        if instr.get("assetType") != "OPTION":
            continue
        sym = (instr.get("symbol") or "")
        if len(sym) < 21:
            continue
        # OSI: 6-char underlying / 6-char YYMMDD / 1-char C|P / 8-digit strike.
        try:
            yymmdd = sym[6:12]
            cp = sym[12]
            strike_int = int(sym[13:21])
            expiry = _date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
            strike = strike_int / 1000.0
        except (ValueError, IndexError):
            continue
        contract = by_key.get((cp, strike))
        if not contract:
            continue
        instruction = (leg.get("instruction") or "").upper()
        sign = 1 if instruction.startswith("BUY") else -1
        qty_int = int(leg.get("quantity") or 0)
        if qty_int == 0:
            continue
        from schwab_cli.commands.strategy import _pick_premium
        premium = _pick_premium(contract)
        priced.append(PricedLeg(
            qty=sign * qty_int, side=cp, expiry=expiry, strike=strike,
            premium=premium, iv=contract.get("iv"),
            delta=contract.get("delta"), gamma=contract.get("gamma"),
            theta=contract.get("theta"), vega=contract.get("vega"),
        ))

    if not priced:
        return None
    if not isinstance(dte, int):
        # Derive from the first leg if envelope didn't carry one.
        from datetime import date as _today_d
        dte = max((priced[0].expiry - _today_d.today()).days, 0)
    return _pop_fn(priced, spot=float(spot), dte=dte)


# ---- preview-response field extraction ------------------------------------


@dataclass(frozen=True)
class PreviewSummary:
    """Schwab-sourced fields the panel surfaces.

    ``bp_after_stock`` and ``bp_after_option`` separate the two TOS
    "Result Buying Power" figures: stock BP includes margin extension,
    option BP is the cash-equivalent that secures option positions.
    Buying-power *effect* is computed at render time as
    ``after - current`` (preview returns only the post-order values).
    """

    commission: float | None
    fees: float | None
    bp_after_stock: float | None        # projectedBuyingPower
    bp_after_option: float | None       # projectedAvailableFund
    warnings: tuple[str, ...]
    rejects: tuple[str, ...]


def summarise_preview(preview: dict | None) -> PreviewSummary:
    """Pluck commission / fees / BP fields and validation messages out of
    Schwab's previewOrder response shape.

    Observed Schwab shape (2026-04):
      commissionAndFee.commission.commissionLegs[*].commissionValues[*].value
      commissionAndFee.fee.feeLegs[*].feeValues[*].value
      orderStrategy.orderBalance.projectedBuyingPower
      orderStrategy.orderBalance.orderValue
      orderValidationResult.{warns,rejects,alerts,reviews}[*].activityMessage
    """
    if not preview:
        return PreviewSummary(None, None, None, None, (), ())

    commission = _sum_commission_legs(
        _path(preview, ["commissionAndFee", "commission", "commissionLegs"])
    )
    fees = _sum_fee_legs(
        _path(preview, ["commissionAndFee", "fee", "feeLegs"])
    )
    bp_after_stock = _to_float(_path(
        preview, ["orderStrategy", "orderBalance", "projectedBuyingPower"]
    ))
    bp_after_option = _to_float(_path(
        preview, ["orderStrategy", "orderBalance", "projectedAvailableFund"]
    ))

    warnings, rejects = _collect_validation_messages(preview)

    return PreviewSummary(
        commission=commission,
        fees=fees,
        bp_after_stock=bp_after_stock,
        bp_after_option=bp_after_option,
        warnings=tuple(warnings),
        rejects=tuple(rejects),
    )


def _sum_commission_legs(legs: Any) -> float | None:
    if not isinstance(legs, list) or not legs:
        return None
    total = 0.0
    seen = False
    for leg in legs:
        for cv in (leg.get("commissionValues") if isinstance(leg, dict) else []) or []:
            v = _to_float(cv.get("value")) if isinstance(cv, dict) else None
            if v is not None:
                total += v
                seen = True
    return total if seen else None


def _sum_fee_legs(legs: Any) -> float | None:
    if not isinstance(legs, list) or not legs:
        return None
    total = 0.0
    seen = False
    for leg in legs:
        for fv in (leg.get("feeValues") if isinstance(leg, dict) else []) or []:
            v = _to_float(fv.get("value")) if isinstance(fv, dict) else None
            if v is not None:
                total += v
                seen = True
    return total if seen else None


def _collect_validation_messages(preview: dict) -> tuple[list[str], list[str]]:
    """Extract human-readable warnings and rejects.

    Schwab uses ``activityMessage`` (with ``message`` as a legacy
    fallback). Buckets observed: ``warns``, ``rejects``, ``alerts``,
    ``reviews``. Anything in ``rejects`` blocks the order; the others
    surface as warnings.
    """
    val = preview.get("orderValidationResult") or {}
    warnings: list[str] = []
    rejects: list[str] = []
    for entry in val.get("rejects") or []:
        msg = _val_message(entry)
        if msg:
            rejects.append(msg)
    for bucket in ("warns", "warnings", "alerts", "reviews"):
        for entry in val.get(bucket) or []:
            msg = _val_message(entry)
            if msg:
                warnings.append(msg)
    return warnings, rejects


def _val_message(entry: object) -> str | None:
    if isinstance(entry, dict):
        return entry.get("activityMessage") or entry.get("message")
    if isinstance(entry, str):
        return entry
    return None


def _path(d: dict, path: list[str]) -> Any:
    cur: Any = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- confirmation panel ---------------------------------------------------


def render_confirmation(
    *,
    body: dict,
    account_tail: str,
    strategy_label: str | None,
    is_naked_short: bool,
    analytics: OrderAnalytics | None,
    preview: PreviewSummary,
    preview_unavailable: bool = False,
    underlying_quote: dict | None = None,
    current_balances: dict | None = None,
    schwab_ticket: str | None = None,
) -> str:
    """Render the TOS-style confirmation panel as a string.

    Caller writes this to **stderr** so ``--json`` on stdout stays
    parseable.
    """
    lines: list[str] = []
    lines.append("=== Confirm Order ".ljust(62, "="))
    lines.append(f"Account:        ********{account_tail}")

    strat_line = strategy_label or _classify_label(body)
    if is_naked_short:
        strat_line += "    ⚠  naked short"
    lines.append(f"Strategy:       {strat_line}")

    order_type = body.get("orderType", "?")
    duration = body.get("duration", "?")
    session = body.get("session", "NORMAL")
    lines.append(
        f"Type/Duration:  {order_type}  /  {duration}  /  {session} session"
    )
    price = body.get("price")
    if price is not None:
        lines.append(f"Price:          {price}  (per {'spread' if _is_multi_leg(body) else 'share'})")

    # Legs
    lines.append("")
    lines.append("Legs")
    lines.append("-" * 62)
    for leg in body.get("orderLegCollection", []):
        lines.append(_format_leg_line(leg))

    # Schwab ticket — copy/paste-back into Schwab's order entry. Inverse
    # of ``--parse``. Caller is responsible for building the string;
    # rendered as its own section so it's easy to mouse-select.
    if schwab_ticket:
        lines.append("")
        lines.append("Schwab Ticket")
        lines.append("-" * 62)
        lines.append(f"  {schwab_ticket}")

    # Width fits the longest current label ("Buying Power (Option):" =
    # 22 chars) plus a 2-char minimum gap to the value column. Wider
    # columns just produce dead air across every panel row.
    _w = 24

    def _row(label: str, value: str) -> str:
        return f"  {(label + ':').ljust(_w)}{value}"

    # Underlying quote (best-effort — caller passes None on miss).
    # The panel section is always a one-shot snapshot taken at panel-
    # build time. For real-place runs the LiveTicker repaints a fresh
    # status line above the confirmation prompt — that's where "live"
    # belongs, not on the panel header which never updates.
    if underlying_quote:
        lines.append("")
        lines.append(f"Underlying  ({underlying_quote.get('symbol', '?')})")
        lines.append("-" * 62)
        last = underlying_quote.get("last")
        bid = underlying_quote.get("bid")
        ask = underlying_quote.get("ask")
        bid_size = underlying_quote.get("bid_size")
        ask_size = underlying_quote.get("ask_size")
        volume = underlying_quote.get("volume")
        net_change = underlying_quote.get("net_change")
        last_str = _fmt_money(last) if last is not None else "—"
        if net_change is not None:
            last_str = f"{last_str}  ({_fmt_money(net_change, plus=True)})"
        lines.append(_row("Last", last_str))
        lines.append(_row(
            "Bid × Size",
            f"{_fmt_money(bid)} × {_fmt_size(bid_size)}",
        ))
        lines.append(_row(
            "Ask × Size",
            f"{_fmt_money(ask)} × {_fmt_size(ask_size)}",
        ))
        lines.append(_row("Volume", _fmt_size(volume)))

    # P&L
    if analytics is not None:
        lines.append("")
        lines.append("P&L (At Expiry)")
        lines.append("-" * 62)
        lines.append(_row(
            "Max Profit",
            _fmt_money(analytics.max_profit, plus=True, unlimited='unlimited (call)'),
        ))
        lines.append(_row(
            "Max Loss",
            _fmt_money(analytics.max_loss, plus=True, unlimited='unlimited (call)'),
        ))
        if analytics.breakevens:
            be = "  ".join(f"${b:,.2f}" for b in analytics.breakevens)
            lines.append(_row("Breakevens", be))
        if analytics.pop is not None:
            lines.append(_row("Prob of Profit", f"{analytics.pop * 100:.1f}%"))
        else:
            lines.append(_row("Prob of Profit", "(unavailable — chain not fetched)"))

    # Cost & Buying Power
    lines.append("")
    lines.append("Cost & Buying Power")
    lines.append("-" * 62)
    if analytics is not None:
        cost_label = "Order Cost" if analytics.order_cost >= 0 else "Order Credit"
        lines.append(_row(cost_label, _fmt_money(abs(analytics.order_cost))))
    if preview_unavailable:
        lines.append(_row("Est. Commission", "unavailable (preview endpoint not enabled)"))
        lines.append(_row("Est. Fees", "unavailable"))
        lines.append(_row("Buying Power (Stock)", "unavailable"))
        lines.append(_row("Buying Power (Option)", "unavailable"))
    else:
        # Schwab's preview returns only post-order values; we pair them
        # with a current-balance fetch upstream and render each BP
        # bucket as ``current → effect → result`` on one column-aligned
        # line. Missing balances (e.g. ``--yes`` skip path) render as
        # "n/a" cells so columns stay aligned.
        #
        # Note: rejected previews still return projected BP values.
        # For *soft* rejects (e.g. "limit far from last price") the
        # projection is real because the operator can override. For
        # *hard* rejects (e.g. unapproved options level) the projection
        # is bogus. We don't yet have a way to tell them apart from
        # the response shape, so we always render — the operator reads
        # the Validation section to decide whether the BP impact is
        # actionable.
        cur_stock = (current_balances or {}).get("stockBuyingPower") if current_balances else None
        cur_option = (current_balances or {}).get("optionBuyingPower") if current_balances else None
        stock_row, option_row = _bp_triples(
            cur_stock, preview.bp_after_stock,
            cur_option, preview.bp_after_option,
        )
        lines.append(_row("Est. Commission", _fmt_money(preview.commission, unlimited='n/a')))
        lines.append(_row("Est. Fees", _fmt_money(preview.fees, unlimited='n/a')))
        lines.append(_row("Buying Power (Stock)", stock_row))
        lines.append(_row("Buying Power (Option)", option_row))

    # Validation
    lines.append("")
    lines.append("Validation")
    lines.append("-" * 62)
    if preview_unavailable:
        lines.append("  ! preview unavailable — Schwab won't pre-validate this order")
    elif preview.rejects:
        for r in preview.rejects:
            lines.append(f"  X {r}")
    elif preview.warnings:
        for w in preview.warnings:
            lines.append(f"  ! {w}")
    else:
        lines.append("  + no warnings from Schwab")

    lines.append("=" * 62)
    return "\n".join(lines) + "\n"


def _classify_label(body: dict) -> str:
    cplx = body.get("complexOrderStrategyType")
    if cplx and cplx != "NONE":
        return cplx
    legs = body.get("orderLegCollection") or []
    if len(legs) == 1:
        leg = legs[0]
        instr = leg.get("instruction", "?")
        sym = (leg.get("instrument") or {}).get("symbol", "?")
        asset = (leg.get("instrument") or {}).get("assetType", "?")
        return f"{instr} {sym}  ({asset})"
    return f"{len(legs)} legs"


def _is_multi_leg(body: dict) -> bool:
    return len(body.get("orderLegCollection") or []) > 1


def _format_leg_line(leg: dict) -> str:
    instruction = leg.get("instruction", "?")
    qty = leg.get("quantity", "?")
    sign = "+" if instruction.startswith("BUY") else "-"
    short_instr = {
        "BUY_TO_OPEN": "BTO", "BUY_TO_CLOSE": "BTC",
        "SELL_TO_OPEN": "STO", "SELL_TO_CLOSE": "STC",
        "BUY": "BUY", "SELL": "SELL",
        "SELL_SHORT": "SS", "BUY_TO_COVER": "BTC",
    }.get(instruction, instruction)
    instrument = leg.get("instrument") or {}
    sym = instrument.get("symbol", "?")
    asset = instrument.get("assetType", "")
    if asset == "OPTION":
        return f"  {sign}{qty}  {short_instr}  {sym}"
    return f"  {sign}{qty}  {short_instr}  {sym}  ({asset})"


def _bp_triples(
    cur_stock: float | None, after_stock: float | None,
    cur_option: float | None, after_option: float | None,
) -> tuple[str, str]:
    """Format Stock + Option BP rows as ``current  effect → result``.

    The semantic shape is *value, action, value* — not a chain — so we
    use a single arrow between the (signed) effect and the resulting
    value. The starting value sits beside the effect with whitespace
    separation. Each column is sized to the wider of the Stock or
    Option cell so the arrows align between rows. Missing values
    render as ``n/a``.
    """
    cur_s_str = _fmt_money(cur_stock, unlimited="n/a")
    cur_o_str = _fmt_money(cur_option, unlimited="n/a")
    eff_stock = _delta(after_stock, cur_stock)
    eff_option = _delta(after_option, cur_option)
    s = (
        cur_s_str,
        _fmt_money(eff_stock, plus=True, unlimited="n/a"),
        _fmt_money(after_stock, unlimited="n/a"),
    )
    o = (
        cur_o_str,
        _fmt_money(eff_option, plus=True, unlimited="n/a"),
        _fmt_money(after_option, unlimited="n/a"),
    )
    widths = tuple(max(len(s[i]), len(o[i])) for i in range(3))

    def _row(cells: tuple[str, str, str]) -> str:
        return (
            f"{cells[0].rjust(widths[0])}  "
            f"{cells[1].rjust(widths[1])}  →  "
            f"{cells[2].rjust(widths[2])}"
        )
    return _row(s), _row(o)


def _delta(after: float | None, before: float | None) -> float | None:
    """Return ``after - before`` when both are numeric; ``None`` otherwise."""
    if after is None or before is None:
        return None
    return after - before


def _fmt_size(v: float | int | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(
    v: float | None, *, plus: bool = False, unlimited: str = "unlimited",
) -> str:
    if v is None:
        return unlimited
    sign = ""
    if v < 0:
        sign = "-"
        v = -v
    elif plus:
        # Always emit "+" for non-negatives (including zero) when plus
        # is requested — keeps signed columns visually balanced.
        sign = "+"
    return f"{sign}${v:,.2f}"


# ---- Schwab ticket renderer (inverse of --parse) -------------------------


_TICKET_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def render_order_ticket(body: dict, *, underlying: str) -> str | None:
    """Build a Schwab/TOS-style order string from an order body.

    Inverse of ``--parse``. Returns a string the operator can copy and
    paste into Schwab's order-entry box, or ``None`` for shapes we
    don't render yet.

    Examples produced::

        BUY +1 NVDA @207.00 LMT
        SELL -1 AMZN 100 1 MAY 26 192.5 PUT @1.65 LMT
        BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [TO CLOSE]
        SELL -1 VERTICAL AMZN 100 1 MAY 26 260/255 CALL @0.85 LMT

    Multi-leg cases reuse :func:`analytics.strategy_ticket.render_ticket`
    by synthesizing per-leg premiums that sum (with sign) to the order's
    net debit/credit price.
    """
    legs = body.get("orderLegCollection") or []
    if not legs:
        return None
    order_type = (body.get("orderType") or "").upper()
    price = body.get("price")
    duration = (body.get("duration") or "").upper()

    # ---- single equity leg --------------------------------------------
    if len(legs) == 1 and (legs[0].get("instrument") or {}).get("assetType") == "EQUITY":
        leg = legs[0]
        instr = leg.get("instruction", "BUY")
        side = "BUY" if instr in ("BUY", "BUY_TO_COVER") else "SELL"
        sign = "+" if side == "BUY" else "-"
        qty = int(leg.get("quantity", 1))
        sym = (leg.get("instrument") or {}).get("symbol", underlying)
        suffix = _ticket_price_suffix(order_type, price, duration)
        return f"{side} {sign}{qty} {sym}{suffix}"

    # ---- option legs (single or multi) --------------------------------
    if not all(
        (leg.get("instrument") or {}).get("assetType") == "OPTION"
        for leg in legs
    ):
        return None  # mixed equity+option not supported in TOS-style yet

    parsed = []
    for leg in legs:
        sym = (leg.get("instrument") or {}).get("symbol", "")
        info = _parse_osi(sym)
        if info is None:
            return None
        _und, expiry, side, strike = info
        instr = leg.get("instruction", "BUY_TO_OPEN")
        sign = 1 if instr.startswith("BUY") else -1
        qty = sign * int(leg.get("quantity", 1))
        # Per-leg effect token. ``positionEffect`` (set when the user
        # was explicit) wins; otherwise infer from instruction. Legs
        # carrying ``*_TO_OPEN`` and no positionEffect are "auto" — the
        # default that the pipeline left untouched.
        pe = leg.get("positionEffect")
        if pe == "CLOSING":
            effect_tok = "close"
        elif pe == "OPENING":
            effect_tok = "open"
        elif instr.endswith("_TO_CLOSE"):
            effect_tok = "close"
        else:
            effect_tok = "auto"
        parsed.append({
            "qty": qty, "side": side, "expiry": expiry, "strike": strike,
            "effect_tok": effect_tok,
        })

    if price is None:
        return None  # MARKET multi-leg — Schwab UI doesn't accept @MKT here
    abs_net = abs(float(price))
    price_tok = f"@{abs_net:.2f} LMT"
    close_tag = _close_marker([p["effect_tok"] for p in parsed])

    # Single-leg option — hand-build the Schwab string.
    if len(parsed) == 1:
        p = parsed[0]
        sign = "+" if p["qty"] > 0 else "-"
        side_word = "BUY" if p["qty"] > 0 else "SELL"
        date_tok = _fmt_ticket_date(p["expiry"])
        weeklys = " (Weeklys)" if not _is_third_friday(p["expiry"]) else ""
        return (
            f"{side_word} {sign}{abs(p['qty'])} {underlying} 100"
            f"{weeklys} {date_tok} {_fmt_ticket_strike(p['strike'])} "
            f"{'CALL' if p['side'] == 'C' else 'PUT'} {price_tok}{close_tag}"
        )

    # Multi-leg — delegate to the existing render_ticket. Synthesize
    # premiums on leg[0] so _net_premium ends up at ±abs_net matching
    # the order's NET_DEBIT (BUY) / NET_CREDIT (SELL).
    from schwab_cli.analytics.strategy import PricedLeg
    from schwab_cli.analytics.strategy_classify import classify
    from schwab_cli.analytics.strategy_legs import Leg
    from schwab_cli.analytics.strategy_ticket import render_ticket

    # Determine side from the *order* type — NET_DEBIT means we pay
    # (BUY), NET_CREDIT means we receive (SELL).
    target_sign = -1 if order_type == "NET_DEBIT" else (
        1 if order_type == "NET_CREDIT" else (
            # Plain LIMIT multi-leg: use sign of first leg.
            -1 if parsed[0]["qty"] > 0 else 1
        )
    )
    # render_ticket's _net_premium = -sum(qty*premium); we want it to
    # equal target_sign * abs_net. So sum(qty*premium) = -target_sign*abs_net.
    needed_sum = -target_sign * abs_net
    head = parsed[0]
    head_premium = (
        needed_sum / head["qty"] if head["qty"] != 0 else 0.0
    )
    priced = []
    for i, p in enumerate(parsed):
        premium = head_premium if i == 0 else 0.0
        priced.append(PricedLeg(
            qty=p["qty"], side=p["side"], expiry=p["expiry"],
            strike=p["strike"], premium=premium,
        ))
    cls_legs = [
        Leg(qty=p["qty"], side=p["side"], expiry=p["expiry"], strike=p["strike"])
        for p in parsed
    ]
    try:
        cls = classify(cls_legs)
        ticket = render_ticket(priced, cls, symbol=underlying)
    except Exception:  # noqa: BLE001 — best-effort; fall through to None
        return None
    return ticket + close_tag


def _close_marker(effects: list[str]) -> str:
    """Build the trailing position-effect marker from a per-leg list of
    effect tokens (each ``"open"``, ``"close"``, or ``"auto"``).

    Rules (mirrors Schwab/TOS conventions):

    * Empty input → ``""``.
    * Every leg "auto" (default — pipeline never touched the leg) →
      ``""`` (omit; the default form has no bracket).
    * Every leg the same explicit value → ``" [TO OPEN]"`` /
      ``" [TO CLOSE]"`` (uniform shorthand).
    * Otherwise → ``" [t1/t2/.../tN]"`` per leg, in body order.
      Tokens render as ``TO OPEN``, ``TO CLOSE``, or ``AUTO``.
    """
    if not effects:
        return ""
    if all(e == "auto" for e in effects):
        return ""
    if all(e == effects[0] for e in effects):
        if effects[0] == "open":
            return " [TO OPEN]"
        if effects[0] == "close":
            return " [TO CLOSE]"
        # Already handled above for "auto".
    parts = []
    for e in effects:
        if e == "open":
            parts.append("TO OPEN")
        elif e == "close":
            parts.append("TO CLOSE")
        else:
            parts.append("AUTO")
    return " [" + "/".join(parts) + "]"


def _parse_osi(sym: str) -> tuple[str, datetime, str, float] | None:
    """Parse a 21-char OSI option symbol into (underlying, expiry, side, strike)."""
    s = sym or ""
    if len(s) < 15:
        return None
    body = s[:6]
    underlying = body.strip()
    yymmdd = s[6:12]
    side = s[12]
    strike_field = s[13:21]
    if not (yymmdd.isdigit() and side in ("C", "P")
            and strike_field.isdigit()):
        return None
    try:
        expiry = datetime(
            2000 + int(yymmdd[:2]),
            int(yymmdd[2:4]),
            int(yymmdd[4:6]),
        )
    except ValueError:
        return None
    strike = int(strike_field) / 1000.0
    return underlying, expiry, side, strike


def _fmt_ticket_date(d) -> str:
    """``D MON YY`` (no leading zero on day)."""
    return f"{d.day} {_TICKET_MONTHS[d.month - 1]} {d.year % 100:02d}"


def _fmt_ticket_strike(strike: float) -> str:
    if strike == int(strike):
        return str(int(strike))
    return f"{strike:.4f}".rstrip("0").rstrip(".")


def _is_third_friday(d) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


def _ticket_price_suffix(order_type: str, price, duration: str) -> str:
    if order_type == "MARKET":
        return " @MKT"
    if price is None:
        return ""
    suffix = f" @{float(price):.2f}"
    if order_type == "LIMIT":
        suffix += " LMT"
    elif order_type == "STOP":
        suffix += " STP"
    elif order_type == "STOP_LIMIT":
        suffix += " STP LMT"
    if duration in ("GOOD_TILL_CANCEL", "GTC"):
        suffix += " GTC"
    return suffix


# ---- order list -----------------------------------------------------------


def render_order_list_human(orders: list[dict]) -> str:
    """Rich-table rendering of `order list` results.

    Order IDs and OSI symbols are emitted in full — truncating Order
    ID broke the displayed value as input to ``order cancel`` /
    ``order get``, and truncating the OSI symbol hid the strike,
    which is the most important leg attribute. OSI's internal
    padding (``KO    260529P00073000``) is collapsed to a single
    space for readability without losing information.
    """
    import re as _re

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=160)
    table = Table(title=f"Orders ({len(orders)})", show_lines=False)
    for col in ("Order ID", "Entered (ET)", "Status", "Type", "Side/Instr",
                "Qty", "Symbol", "Price"):
        table.add_column(col)
    for o in orders:
        order_id = str(o.get("orderId", "?"))
        entered = _fmt_iso_to_et(o.get("enteredTime"))
        status = o.get("status", "?")
        otype = o.get("orderType", "?")
        legs = o.get("orderLegCollection") or []
        instrs = "/".join(l.get("instruction", "?")[:3] for l in legs[:3])
        if len(legs) > 3:
            instrs += "/+"
        qty = sum(l.get("quantity", 0) for l in legs) or o.get("quantity", "?")
        symbols = ",".join(
            _re.sub(r"\s+", " ",
                    (l.get("instrument") or {}).get("symbol") or "?")
            for l in legs[:2]
        )
        if len(legs) > 2:
            symbols += ",..."
        price = o.get("price")
        price_s = "MKT" if price in (None, 0) and otype == "MARKET" else (
            f"{price}" if price is not None else "-"
        )
        table.add_row(
            order_id, entered, status, otype, instrs,
            str(qty), symbols, price_s,
        )
    console.print(table)
    return buf.getvalue()


def render_order_list_json(orders: list[dict]) -> str:
    return _json.dumps(orders, default=str, indent=2) + "\n"


def render_order_detail_human(order: dict) -> str:
    """Focused view for `order get`."""
    lines: list[str] = []
    lines.append(f"=== Order {order.get('orderId', '?')} ".ljust(62, "="))
    lines.append(f"Status:         {order.get('status', '?')}")
    lines.append(f"Type:           {order.get('orderType', '?')}  "
                 f"{order.get('duration', '?')}  "
                 f"{order.get('session', '?')} session")
    if order.get("price") is not None:
        lines.append(f"Price:          {order['price']}")
    lines.append(f"Entered:        {_fmt_iso_to_et(order.get('enteredTime'))} ET")
    if order.get("closeTime"):
        lines.append(f"Closed:         {_fmt_iso_to_et(order.get('closeTime'))} ET")
    if order.get("filledQuantity"):
        lines.append(f"Filled qty:     {order['filledQuantity']}")
    lines.append("")
    lines.append("Legs")
    lines.append("-" * 62)
    for leg in order.get("orderLegCollection") or []:
        lines.append(_format_leg_line(leg))
    lines.append("=" * 62)
    return "\n".join(lines) + "\n"


def render_order_detail_json(order: dict) -> str:
    return _json.dumps(order, default=str, indent=2) + "\n"


def _fmt_iso_to_et(s: Any) -> str:
    if not isinstance(s, str):
        return "?"
    try:
        # Schwab emits both '...Z' and '...+0000' shapes. Handle both.
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return s
