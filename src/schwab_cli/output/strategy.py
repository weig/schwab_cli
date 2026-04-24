"""Renderer for the ``strategy`` command.

Consumes the metrics envelope produced by
:mod:`schwab_cli.commands.strategy` (which wires the analytics,
classifier, and ticket renderer into one dict) and produces HUMAN,
JSON, or MD output.

Convention highlights:

* ``max_loss`` serialises as the string ``"unlimited"`` in JSON when
  the analytics layer returned ``None`` — avoids ``null`` ambiguity
  and ``-Infinity`` for agents parsing the output.
* HUMAN always uses text labels (``Net Credit``, ``Net Debit``,
  ``unlimited``) rather than exposing the signed ``net_premium``
  convention directly.
* When ``supported`` is ``False`` we render legs + Schwab ticket +
  warnings but skip the P/L / POP / EV block — no fake numbers.
"""

from __future__ import annotations

import json as _json
from typing import Any

from schwab_cli.output.format import Format


# ---- small formatters --------------------------------------------------


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _money_signed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _pct(v: float | None, places: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{places}f}%"


def _pct_from_decimal(v: float | None, places: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{places}f}%"


def _signed(v: float | None, fmt: str = "+.2f") -> str:
    if v is None:
        return "—"
    return f"{v:{fmt}}"


def _strike(v: float) -> str:
    if v == int(v):
        return f"${int(v):,}"
    return f"${v:,.2f}"


def _premium_label(net_premium: float | None, credit: float, debit: float) -> str:
    """Emit 'Net Credit $1.30' / 'Net Debit $2.00' / 'Net Even $0.00'."""
    if net_premium is None:
        return "Net —"
    if credit > 0 and debit == 0:
        return f"Net Credit ${credit:,.2f}"
    if debit > 0 and credit == 0:
        return f"Net Debit ${debit:,.2f}"
    return "Net Even $0.00"


# ---- top-level dispatch ------------------------------------------------


def render_strategy(m: dict[str, Any], *, fmt: Format) -> str:
    if fmt is Format.JSON:
        return _render_json(m)
    if fmt is Format.MD:
        return _render_md(m)
    return _render_human(m)


# ---- HUMAN -------------------------------------------------------------


def _render_human(m: dict[str, Any]) -> str:
    out: list[str] = []
    sym = m.get("symbol") or ""
    strategy = m.get("strategy") or "Strategy"
    dte = m.get("dte")
    dte_str = f"DTE {dte}" if dte is not None else "DTE —"
    exp_str = _shared_expiry_str(m)

    header = f"=== {sym} {strategy}"
    if exp_str:
        header += f" — exp {exp_str}"
    header += f" ({dte_str}) ==="
    out.append(header)

    spot_line = f"Spot: {_money(m.get('spot'))}"
    model = m.get("model")
    if model:
        spot_line += f"   |   Model: {model}"
    out.append(spot_line)
    out.append("")

    # Legs table.
    out.append("Legs:")
    for leg in m.get("legs") or []:
        qty = leg.get("qty", 0)
        qty_tok = f"+{qty}" if qty > 0 else str(qty)
        side = "CALL" if leg.get("side") == "C" else "PUT"
        strike = leg.get("strike")
        iv = leg.get("iv_pct")
        delta = leg.get("delta")
        premium = leg.get("premium")
        prem_signed = premium if qty > 0 else -premium if premium is not None else None
        out.append(
            f"  {qty_tok:>3} {side:<4} {_strike(strike):>10}   "
            f"Δ {_signed(delta):>6}   IV {_pct(iv):>6}   "
            f"premium {_money_signed(-prem_signed) if prem_signed is not None else '—':>10}"
        )
    # Net premium line under the legs, right-aligned.
    label = _premium_label(m.get("net_premium"), m.get("net_credit", 0.0), m.get("net_debit", 0.0))
    out.append("                                                 " + "─" * 20)
    out.append("                                                 " + label)
    out.append("")

    # Schwab ticket.
    ticket = m.get("ticket")
    if ticket:
        out.append("Schwab order ticket (copy-paste):")
        out.append(f"  {ticket}")
        out.append("")

    # Metrics block — only if supported.
    if m.get("supported"):
        out.append("Outcome Metrics:")
        out.append(f"  POP:   {_pct_from_decimal(m.get('pop'))}")
        out.append(f"  EV:    {_money_signed(m.get('ev'))}")
        out.append(f"  Max Profit: {_money(m.get('max_profit')) if m.get('max_profit') is not None else 'unlimited'}")
        out.append(
            f"  Max Loss:   "
            f"{_money(m.get('max_loss')) if m.get('max_loss') is not None else 'unlimited'}"
        )
        bes = m.get("breakevens") or []
        if bes:
            pts = m.get("prob_touch") or []
            out.append("  Breakevens:")
            spot = m.get("spot") or 0.0
            for i, be in enumerate(bes):
                dist = be - spot
                pct = (dist / spot) * 100 if spot else None
                touch_str = ""
                if i < len(pts) and pts[i] is not None:
                    touch_str = f"   P(touch) {_pct_from_decimal(pts[i])}"
                out.append(
                    f"    {_money(be)}   "
                    f"({_signed(dist, '+,.2f')} / {_signed(pct, '+.1f')}%)"
                    f"{touch_str}"
                )
        greeks = m.get("greeks") or {}
        g_parts = []
        if greeks.get("delta") is not None:
            g_parts.append(f"Δ {_signed(greeks['delta'], '+.3f')}")
        if greeks.get("gamma") is not None:
            g_parts.append(f"Γ {_signed(greeks['gamma'], '+.4f')}")
        if greeks.get("theta") is not None:
            g_parts.append(f"Θ {_money_signed(greeks['theta'])}/day")
        if greeks.get("vega") is not None:
            g_parts.append(f"ν {_money_signed(greeks['vega'])}/vol pt")
        if g_parts:
            out.append(f"  Greeks: {'   '.join(g_parts)}")
        out.append("")
    else:
        reason = m.get("reason") or "this shape"
        out.append(f"Analytics: not supported yet ({reason}) — Phase 2.")
        out.append("")

    # Warnings.
    warnings = m.get("warnings") or []
    if warnings:
        out.append("⚠ Warnings:")
        for w in warnings:
            out.append(f"  {w}")

    return "\n".join(out).rstrip() + "\n"


# ---- JSON --------------------------------------------------------------


def _render_json(m: dict[str, Any]) -> str:
    # Shallow copy; replace None max_loss with "unlimited" sentinel
    # when the analytics layer flagged unbounded exposure.
    envelope = dict(m)
    if (
        envelope.get("max_loss") is None
        and "unlimited_loss" in (envelope.get("warnings") or [])
    ):
        envelope["max_loss"] = "unlimited"
    if (
        envelope.get("max_profit") is None
        and envelope.get("supported")
    ):
        # Single long leg ⇒ unlimited profit on the right. Reflect same
        # convention for symmetry.
        envelope["max_profit"] = "unlimited"
    return _json.dumps(envelope, indent=2, default=str)


# ---- MD ----------------------------------------------------------------


def _render_md(m: dict[str, Any]) -> str:
    lines: list[str] = []
    sym = m.get("symbol") or ""
    strategy = m.get("strategy") or "Strategy"
    dte = m.get("dte")
    lines.append(f"# {sym} {strategy}")
    lines.append("")
    meta = [f"**Spot:** {_money(m.get('spot'))}"]
    if dte is not None:
        meta.append(f"**DTE:** {dte}")
    if m.get("model"):
        meta.append(f"**Model:** `{m['model']}`")
    lines.append("  |  ".join(meta))
    lines.append("")

    # Ticket.
    ticket = m.get("ticket")
    if ticket:
        lines.append("## Schwab Order Ticket")
        lines.append("")
        lines.append("```")
        lines.append(ticket)
        lines.append("```")
        lines.append("")

    # Legs.
    lines.append("## Legs")
    lines.append("")
    lines.append("| Qty | Side | Strike | Expiry | IV | Δ | Premium |")
    lines.append("| ---: | --- | ---: | --- | ---: | ---: | ---: |")
    for leg in m.get("legs") or []:
        qty = leg.get("qty", 0)
        sign_qty = f"+{qty}" if qty > 0 else str(qty)
        side = "CALL" if leg.get("side") == "C" else "PUT"
        strike = leg.get("strike")
        exp = leg.get("expiry") or ""
        iv = leg.get("iv_pct")
        delta = leg.get("delta")
        premium = leg.get("premium")
        prem_signed = premium if qty > 0 else -premium if premium is not None else None
        lines.append(
            f"| {sign_qty} | {side} | {_strike(strike)} | `{exp}` | "
            f"{_pct(iv)} | {_signed(delta)} | "
            f"{_money_signed(-prem_signed) if prem_signed is not None else '—'} |"
        )
    lines.append("")
    label = _premium_label(m.get("net_premium"), m.get("net_credit", 0.0), m.get("net_debit", 0.0))
    lines.append(f"**{label}**")
    lines.append("")

    # Metrics.
    lines.append("## Metrics")
    lines.append("")
    if m.get("supported"):
        lines.append("| Metric | Value |")
        lines.append("| --- | ---: |")
        lines.append(f"| POP | {_pct_from_decimal(m.get('pop'))} |")
        lines.append(f"| EV | {_money_signed(m.get('ev'))} |")
        mp = m.get("max_profit")
        ml = m.get("max_loss")
        lines.append(f"| Max Profit | {_money(mp) if mp is not None else 'unlimited'} |")
        lines.append(f"| Max Loss | {_money(ml) if ml is not None else 'unlimited'} |")
        bes = m.get("breakevens") or []
        if bes:
            lines.append(f"| Breakevens | {', '.join(_money(b) for b in bes)} |")
        greeks = m.get("greeks") or {}
        if any(greeks.get(k) is not None for k in ("delta", "gamma", "theta", "vega")):
            g_parts = []
            if greeks.get("delta") is not None:
                g_parts.append(f"Δ {_signed(greeks['delta'], '+.3f')}")
            if greeks.get("gamma") is not None:
                g_parts.append(f"Γ {_signed(greeks['gamma'], '+.4f')}")
            if greeks.get("theta") is not None:
                g_parts.append(f"Θ {_money_signed(greeks['theta'])}/day")
            if greeks.get("vega") is not None:
                g_parts.append(f"ν {_money_signed(greeks['vega'])}/vol pt")
            lines.append(f"| Greeks | {', '.join(g_parts)} |")
    else:
        reason = m.get("reason") or "this shape"
        lines.append(f"_Analytics not supported yet ({reason}) — Phase 2._")
    lines.append("")

    warnings = m.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- `{w}`")

    return "\n".join(lines).rstrip() + "\n"


# ---- helpers -----------------------------------------------------------


def _shared_expiry_str(m: dict[str, Any]) -> str:
    legs = m.get("legs") or []
    expiries = {leg.get("expiry") for leg in legs if leg.get("expiry")}
    if len(expiries) == 1:
        return next(iter(expiries))
    return ""
