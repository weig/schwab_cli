"""Renderers for the ``skew`` command.

Three rendering entry points, one per mode:

* :func:`render_skew` — L1: single-chain skew table.
* :func:`render_term` — L2: term-structure table across DTE.
* :func:`render_cross` — L3: cross-ticker table at a shared DTE.

Each renderer accepts a :class:`Format` and returns a string. Output
shapes match the ``skew`` command spec §4 (text for HUMAN, field-stable
envelope for JSON, GFM for MD).
"""

from __future__ import annotations

import json as _json
from typing import Any

from schwab_cli.output.format import Format


# ---- small formatters --------------------------------------------------


def _sign(v: float | None, fmt: str = "+.2f") -> str:
    """Signed number with em-dash fallback for ``None``."""
    if v is None:
        return "—"
    return f"{v:{fmt}}"


def _pct(v: float | None, places: int = 2) -> str:
    """Non-signed percent (for IV, already in 0-100 scale)."""
    if v is None:
        return "—"
    return f"{v:.{places}f}%"


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _lean(rr: float | None) -> str:
    """Human tag for a risk-reversal value."""
    if rr is None:
        return "—"
    if rr > 0:
        return "put premium"
    if rr < 0:
        return "call premium"
    return "flat"


def _shape(bf: float | None) -> str:
    """Human tag for a butterfly value."""
    if bf is None:
        return "—"
    if bf > 0:
        return "convex smile"
    if bf < 0:
        return "inverted smile"
    return "flat"


def _slope_tag(slope: float | None) -> str:
    if slope is None:
        return "—"
    if slope < 0:
        return "put skew"
    if slope > 0:
        return "call skew"
    return "flat"


# ---- L1: single chain --------------------------------------------------


def render_skew(metrics: dict[str, Any], *, fmt: Format) -> str:
    if fmt is Format.JSON:
        return _json.dumps(metrics, indent=2, default=str)
    if fmt is Format.MD:
        return _render_l1_md(metrics)
    return _render_l1_human(metrics)


def _render_l1_human(m: dict[str, Any]) -> str:
    out: list[str] = []
    dte = m.get("dte")
    dte_str = f"DTE {dte}" if dte is not None else "DTE —"
    out.append(f"=== {m['symbol']} Skew — exp {m['expiry']} ({dte_str}) ===")
    out.append(f"Spot: {_money(m.get('spot'))}")
    out.append("")

    atm = m.get("atm") or {}
    if atm.get("iv_pct") is not None:
        out.append(
            f"ATM  strike {_money(atm.get('strike'))}   "
            f"IV {_pct(atm['iv_pct'])}"
        )
        out.append("")

    for label, d in [("25Δ", m.get("d25") or {}), ("10Δ", m.get("d10") or {})]:
        p, c = d.get("put"), d.get("call")
        if not (p and c):
            continue
        out.append(f"{label} Skew:")
        out.append(
            f"  Put   K {_money(p.get('strike'))}   "
            f"Δ {_sign(p.get('delta'))}   IV {_pct(p.get('iv_pct'))}"
        )
        out.append(
            f"  Call  K {_money(c.get('strike'))}   "
            f"Δ {_sign(c.get('delta'))}   IV {_pct(c.get('iv_pct'))}"
        )
        rr = d.get("rr")
        if rr is not None:
            out.append(f"  Risk Reversal:  {_sign(rr)} vol pt   ({_lean(rr)})")
        bf = d.get("bf")
        if bf is not None:
            out.append(f"  Butterfly:      {_sign(bf)} vol pt   ({_shape(bf)})")
        out.append("")

    slope = m.get("atm_slope_per_dollar")
    if slope is not None:
        out.append(
            f"ATM Slope:  {_sign(slope, '+.4f')} vol pt / $1   "
            f"({_sign(slope * 10)} per $10, {_slope_tag(slope)})"
        )

    rng = m.get("iv_range") or {}
    if rng.get("min_pct") is not None:
        out.append(
            f"IV Range:   {_pct(rng['min_pct'])} – {_pct(rng['max_pct'])}   "
            f"(spread {_sign(rng['spread_pct'])} pt)"
        )

    return "\n".join(out) + "\n"


def _render_l1_md(m: dict[str, Any]) -> str:
    dte = m.get("dte")
    lines: list[str] = []
    lines.append(f"# {m['symbol']} Skew — `{m['expiry']}` (DTE {dte if dte is not None else '—'})")
    lines.append("")
    lines.append(f"**Spot:** {_money(m.get('spot'))}")
    lines.append("")

    atm = m.get("atm") or {}
    if atm.get("iv_pct") is not None:
        lines.append(
            f"**ATM:** strike {_money(atm.get('strike'))}, "
            f"IV {_pct(atm['iv_pct'])}"
        )
        lines.append("")

    lines.append("## Skew Legs")
    lines.append("")
    lines.append("| Leg | Strike | Δ | IV |")
    lines.append("| --- | ---: | ---: | ---: |")
    for label, leg in [
        ("25Δ Put", (m.get("d25") or {}).get("put")),
        ("25Δ Call", (m.get("d25") or {}).get("call")),
        ("10Δ Put", (m.get("d10") or {}).get("put")),
        ("10Δ Call", (m.get("d10") or {}).get("call")),
    ]:
        if leg:
            lines.append(
                f"| {label} | {_money(leg.get('strike'))} | "
                f"{_sign(leg.get('delta'))} | {_pct(leg.get('iv_pct'))} |"
            )
    lines.append("")

    lines.append("## Derived Metrics")
    lines.append("")
    lines.append("| Metric | Value | Interpretation |")
    lines.append("| --- | ---: | --- |")
    rr25 = (m.get("d25") or {}).get("rr")
    bf25 = (m.get("d25") or {}).get("bf")
    rr10 = (m.get("d10") or {}).get("rr")
    bf10 = (m.get("d10") or {}).get("bf")
    slope = m.get("atm_slope_per_dollar")
    rng = m.get("iv_range") or {}

    if rr25 is not None:
        lines.append(f"| 25Δ Risk Reversal | {_sign(rr25)} vol pt | {_lean(rr25)} |")
    if bf25 is not None:
        lines.append(f"| 25Δ Butterfly | {_sign(bf25)} vol pt | {_shape(bf25)} |")
    if rr10 is not None:
        lines.append(f"| 10Δ Wing Skew | {_sign(rr10)} vol pt | {_lean(rr10)} |")
    if bf10 is not None:
        lines.append(f"| 10Δ Butterfly | {_sign(bf10)} vol pt | {_shape(bf10)} |")
    if slope is not None:
        lines.append(
            f"| ATM Slope | {_sign(slope, '+.4f')} vol pt / $1 | {_slope_tag(slope)} |"
        )
    if rng.get("spread_pct") is not None:
        lines.append(
            f"| IV Range | {_pct(rng['min_pct'])} – {_pct(rng['max_pct'])} | "
            f"{_sign(rng['spread_pct'])} pt spread |"
        )

    return "\n".join(lines) + "\n"


# ---- L2: term structure ------------------------------------------------


def render_term(
    metrics: list[dict[str, Any]],
    *,
    fmt: Format,
    symbol: str,
) -> str:
    if fmt is Format.JSON:
        return _json.dumps(metrics, indent=2, default=str)
    if fmt is Format.MD:
        return _render_term_md(metrics, symbol)
    return _render_term_human(metrics, symbol)


def _term_rows(metrics: list[dict[str, Any]]) -> list[tuple[str, Any, Any, Any, Any, Any]]:
    """Project each L1 metric into the (expiry, dte, atm_iv, rr, bf, slope)
    tuple the L2 renderers consume."""
    rows: list[tuple[str, Any, Any, Any, Any, Any]] = []
    for m in metrics:
        atm_iv = (m.get("atm") or {}).get("iv_pct")
        rr = (m.get("d25") or {}).get("rr")
        bf = (m.get("d25") or {}).get("bf")
        slope = m.get("atm_slope_per_dollar")
        rows.append((m.get("expiry") or "—", m.get("dte"), atm_iv, rr, bf, slope))
    return rows


def _render_term_human(metrics: list[dict[str, Any]], symbol: str) -> str:
    if not metrics:
        return f"=== {symbol} Term Structure ===\nNo data.\n"
    rows = _term_rows(metrics)
    lines: list[str] = [
        f"=== {symbol} Term Structure ===",
        f"{'Expiry':<12}{'DTE':>5}{'ATM IV':>10}{'25Δ RR':>10}{'25Δ BF':>10}{'Slope/$':>12}",
        "-" * 59,
    ]
    for exp, dte, atm_iv, rr, bf, slope in rows:
        lines.append(
            f"{exp:<12}"
            f"{(str(dte) if dte is not None else '—'):>5}"
            f"{_pct(atm_iv, 1):>10}"
            f"{_sign(rr):>10}"
            f"{_sign(bf):>10}"
            f"{_sign(slope, '+.4f'):>12}"
        )
    return "\n".join(lines) + "\n"


def _render_term_md(metrics: list[dict[str, Any]], symbol: str) -> str:
    lines: list[str] = [f"# {symbol} Term Structure — Skew", ""]
    if not metrics:
        lines.append("_No data._")
        return "\n".join(lines) + "\n"
    lines.append("| Expiry | DTE | ATM IV | 25Δ RR | 25Δ BF | Slope/$ |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for exp, dte, atm_iv, rr, bf, slope in _term_rows(metrics):
        lines.append(
            f"| `{exp}` | {dte if dte is not None else '—'} | "
            f"{_pct(atm_iv, 1)} | {_sign(rr)} | {_sign(bf)} | "
            f"{_sign(slope, '+.4f')} |"
        )
    return "\n".join(lines) + "\n"


# ---- L3: cross-ticker --------------------------------------------------


def render_cross(metrics: list[dict[str, Any]], *, fmt: Format) -> str:
    if fmt is Format.JSON:
        return _json.dumps(metrics, indent=2, default=str)
    if fmt is Format.MD:
        return _render_cross_md(metrics)
    return _render_cross_human(metrics)


def _cross_rows(metrics: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for m in metrics:
        atm_iv = (m.get("atm") or {}).get("iv_pct")
        rr25 = (m.get("d25") or {}).get("rr")
        rr10 = (m.get("d10") or {}).get("rr")
        bf25 = (m.get("d25") or {}).get("bf")
        slope = m.get("atm_slope_per_dollar")
        rows.append(
            (m.get("symbol") or "—", m.get("dte"), atm_iv, rr25, rr10, bf25, slope)
        )
    return rows


def _render_cross_human(metrics: list[dict[str, Any]]) -> str:
    if not metrics:
        return "=== Cross-Ticker Skew ===\nNo data.\n"
    # Header uses the DTE of the first row as a hint — the L3 assumption
    # is that all chains share a close-enough DTE.
    dte_hint = metrics[0].get("dte")
    head = f"=== Cross-Ticker Skew (DTE ~{dte_hint if dte_hint is not None else '—'}) ==="
    lines: list[str] = [
        head,
        f"{'Ticker':<8}{'DTE':>5}{'ATM IV':>10}{'25Δ RR':>10}"
        f"{'10Δ Wing':>11}{'25Δ BF':>10}{'Slope/$':>12}",
        "-" * 66,
    ]
    for sym, dte, atm_iv, rr25, rr10, bf25, slope in _cross_rows(metrics):
        lines.append(
            f"{sym:<8}"
            f"{(str(dte) if dte is not None else '—'):>5}"
            f"{_pct(atm_iv, 1):>10}"
            f"{_sign(rr25):>10}"
            f"{_sign(rr10):>11}"
            f"{_sign(bf25):>10}"
            f"{_sign(slope, '+.4f'):>12}"
        )
    return "\n".join(lines) + "\n"


def _render_cross_md(metrics: list[dict[str, Any]]) -> str:
    if not metrics:
        return "# Cross-Ticker Skew\n\n_No data._\n"
    dte_hint = metrics[0].get("dte")
    lines: list[str] = [
        f"# Cross-Ticker Skew (DTE ~{dte_hint if dte_hint is not None else '—'})",
        "",
        "| Ticker | DTE | ATM IV | 25Δ RR | 10Δ Wing | 25Δ BF | Slope/$ |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sym, dte, atm_iv, rr25, rr10, bf25, slope in _cross_rows(metrics):
        lines.append(
            f"| {sym} | {dte if dte is not None else '—'} | "
            f"{_pct(atm_iv, 1)} | {_sign(rr25)} | {_sign(rr10)} | "
            f"{_sign(bf25)} | {_sign(slope, '+.4f')} |"
        )
    return "\n".join(lines) + "\n"
