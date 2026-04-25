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
    """The four Schwab-sourced fields the panel surfaces."""

    commission: float | None
    fees: float | None
    bp_effect: float | None       # negative = consumed BP
    bp_after: float | None
    warnings: tuple[str, ...]
    rejects: tuple[str, ...]


def summarise_preview(preview: dict | None) -> PreviewSummary:
    """Pluck commission / fees / BP fields and validation messages out of
    Schwab's previewOrder response shape.

    Schwab's exact field paths shift between response shapes; we try
    a couple of common locations and fall through to ``None`` on miss.
    """
    if not preview:
        return PreviewSummary(None, None, None, None, (), ())

    commission = _first(preview, [
        ["commission"],
        ["orderValueImpact", "commission"],
        ["orderFees", "commission"],
    ])
    fees = _first(preview, [
        ["fees"],
        ["orderValueImpact", "fees"],
        ["orderFees", "fees"],
    ])
    bp_effect = _first(preview, [
        ["orderValueImpact", "buyingPowerEffect"],
        ["buyingPowerEffect"],
        ["accountImpact", "buyingPowerEffect"],
    ])
    bp_after = _first(preview, [
        ["orderValueImpact", "buyingPowerAfter"],
        ["buyingPowerAfter"],
        ["accountImpact", "buyingPowerAfter"],
    ])

    warnings: list[str] = []
    rejects: list[str] = []
    val = preview.get("orderValidationResult") or {}
    for w in val.get("warnings") or []:
        msg = w.get("message") if isinstance(w, dict) else str(w)
        if msg:
            warnings.append(msg)
    for r in val.get("rejects") or []:
        msg = r.get("message") if isinstance(r, dict) else str(r)
        if msg:
            rejects.append(msg)

    return PreviewSummary(
        commission=_to_float(commission),
        fees=_to_float(fees),
        bp_effect=_to_float(bp_effect),
        bp_after=_to_float(bp_after),
        warnings=tuple(warnings),
        rejects=tuple(rejects),
    )


def _first(d: dict, paths: list[list[str]]) -> Any:
    for path in paths:
        cur: Any = d
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


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
) -> str:
    """Render the TOS-style confirmation panel as a string.

    Caller writes this to **stderr** so ``--json`` on stdout stays
    parseable.
    """
    lines: list[str] = []
    lines.append("=== Confirm order ".ljust(62, "="))
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

    # P&L
    if analytics is not None:
        lines.append("")
        lines.append("P&L (at expiry)")
        lines.append("-" * 62)
        lines.append(
            f"  Max profit:        {_fmt_money(analytics.max_profit, plus=True, unlimited='unlimited (call)')}"
        )
        lines.append(
            f"  Max loss:          {_fmt_money(analytics.max_loss, plus=True, unlimited='unlimited (call)')}"
        )
        if analytics.breakevens:
            be = "  ".join(f"${b:,.2f}" for b in analytics.breakevens)
            lines.append(f"  Breakevens:        {be}")
        lines.append(
            "  POP:               (deferred to Phase 2 — needs live IV)"
        )

    # Cost & buying power
    lines.append("")
    lines.append("Cost & buying power")
    lines.append("-" * 62)
    if analytics is not None:
        cost_label = "Order cost" if analytics.order_cost >= 0 else "Order credit"
        lines.append(f"  {cost_label}:        {_fmt_money(abs(analytics.order_cost))}")
    if preview_unavailable:
        lines.append("  Est. commission:   unavailable (preview endpoint not enabled)")
        lines.append("  Est. fees:         unavailable")
        lines.append("  BP effect:         unavailable")
        lines.append("  BP after order:    unavailable")
    else:
        lines.append(f"  Est. commission:   {_fmt_money(preview.commission)}")
        lines.append(f"  Est. fees:         {_fmt_money(preview.fees)}")
        lines.append(f"  BP effect:         {_fmt_money(preview.bp_effect, plus=True)}")
        lines.append(f"  BP after order:    {_fmt_money(preview.bp_after)}")

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
