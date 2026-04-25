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

    POP is deferred to the Phase 2 live-MCP integration (needs a
    real-time IV pull). For Phase 1 we surface only the deterministic
    payoff-at-expiry numbers.
    """

    max_profit: float | None    # dollars; None = unlimited
    max_loss: float | None      # dollars (negative); None = unlimited downside
    breakevens: tuple[float, ...]
    order_cost: float           # dollars; positive = debit, negative = credit


def compute_analytics(
    *,
    strategy: str | None,
    side: str,           # BUY / SELL
    option_type: str | None,  # CALL / PUT (None for equity)
    strikes: tuple[float, ...],
    quantity: int,
    price: float | None,
) -> OrderAnalytics | None:
    """Return :class:`OrderAnalytics` for option orders we know how to
    score, or ``None`` for equity / unsupported shapes.

    ``price`` is the limit price per share (single leg) or per spread
    (vertical). For market orders we can't compute these → return ``None``.
    """
    if not option_type or price is None:
        return None
    qty_mult = 100 * quantity

    if strategy is None:
        # Single-leg option.
        K = strikes[0]
        if side == "BUY":  # long
            cost = price * qty_mult
            if option_type == "CALL":
                return OrderAnalytics(
                    max_profit=None,                # unlimited upside
                    max_loss=-cost,
                    breakevens=(K + price,),
                    order_cost=cost,
                )
            else:  # PUT
                return OrderAnalytics(
                    max_profit=(K - price) * qty_mult,
                    max_loss=-cost,
                    breakevens=(K - price,),
                    order_cost=cost,
                )
        else:  # SELL
            credit = price * qty_mult
            if option_type == "CALL":
                return OrderAnalytics(
                    max_profit=credit,
                    max_loss=None,                  # unlimited
                    breakevens=(K + price,),
                    order_cost=-credit,
                )
            else:  # PUT
                return OrderAnalytics(
                    max_profit=credit,
                    max_loss=-(K - price) * qty_mult,
                    breakevens=(K - price,),
                    order_cost=-credit,
                )

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
            return OrderAnalytics(
                max_profit=max_profit,
                max_loss=max_loss,
                breakevens=(breakeven,),
                order_cost=debit_per * qty_mult,
            )
        else:  # SELL — credit spread
            credit_per = price
            max_profit = credit_per * qty_mult
            max_loss = -(width - credit_per) * qty_mult
            if option_type == "CALL":
                breakeven = lower + credit_per
            else:
                breakeven = higher - credit_per
            return OrderAnalytics(
                max_profit=max_profit,
                max_loss=max_loss,
                breakevens=(breakeven,),
                order_cost=-credit_per * qty_mult,
            )

    return None


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

    # Width fits the longest label below ("Result Buying Power (Option):").
    _w = 32

    def _row(label: str, value: str) -> str:
        return f"  {(label + ':').ljust(_w)}{value}"

    # Underlying quote (best-effort — caller passes None on miss).
    # Header marks the snapshot as "Live Quote" for real-place reviews
    # vs "Quote" for previews so the operator knows the freshness budget.
    if underlying_quote:
        lines.append("")
        kind = "Live Quote" if underlying_quote.get("is_live") else "Quote"
        lines.append(f"Underlying  ({underlying_quote.get('symbol', '?')} — {kind})")
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
        lines.append(_row("POP", "(deferred to Phase 2 — needs live IV)"))

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
    """Format Stock + Option BP rows as ``current → effect → result``.

    Each of the three money columns is sized to the wider of the Stock
    or Option cell so the arrows line up between the two rows without
    wasting space. Missing values render as ``n/a``.
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
            f"{cells[0].rjust(widths[0])}  →  "
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
    if plus and v > 0:
        sign = "+"
    elif v < 0:
        sign = "-"
        v = -v
    return f"{sign}${v:,.2f}"


# ---- order list -----------------------------------------------------------


def render_order_list_human(orders: list[dict]) -> str:
    """Rich-table rendering of `order list` results."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=140)
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
            ((l.get("instrument") or {}).get("symbol") or "?")[:14] for l in legs[:2]
        )
        if len(legs) > 2:
            symbols += ",..."
        price = o.get("price")
        price_s = "MKT" if price in (None, 0) and otype == "MARKET" else (
            f"{price}" if price is not None else "-"
        )
        table.add_row(
            order_id[-10:], entered, status, otype, instrs,
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
