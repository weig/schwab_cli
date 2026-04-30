"""Renderers for ``fundamentals`` command.

The Schwab ``/quotes?fields=quote,fundamental`` endpoint returns a per-
symbol ``fundamental`` block. Schwab reports margin / return figures and
``dividendYield`` as **percentage values** (e.g. ``46.86`` for 46.86%,
``0.44`` for 0.44%). We surface them as-is plus a ``%`` suffix — do not
multiply by 100 again.

Human mode stacks a metric/value table per symbol (multi-symbol layouts
exceed terminal width). MD mode renders one row per symbol with the
headline metrics for at-a-glance comparison. JSON surfaces the raw
fundamental block plus the last price for downstream tooling.

P/E semantics
-------------
Schwab's ``peRatio`` field is **forward / normalized** (it does NOT
equal ``last / epsTTM`` for any growing company). The ``epsTTM`` field
is the trailing 12-month figure, sourced from a different basis. To
keep downstream consumers from confusing the two we surface a derived
``valuation`` section alongside the raw ``fundamental`` block::

    valuation = {
        "pe_forward": fundamental.peRatio,             # Schwab's forward
        "pe_ttm":     last / fundamental.epsTTM,       # derived TTM
        "eps_ttm":    fundamental.epsTTM,
    }

Data quality
------------
Dual-class tickers (BRK.A/B, BF.A/B, …) are sometimes served with the
sister-class EPS leaked into the response (Schwab upstream bug). We
detect that with a deliberately conservative heuristic — symbol
contains ``/`` AND ``epsTTM > 1000`` — and append a non-fatal entry
to ``data_quality_warnings``. We do NOT mutate the numbers, since
Schwab may fix the bug at any time and a hard-coded share-class ratio
would silently corrupt correct data.
"""

from __future__ import annotations

import json as _json
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _num(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v: Any, decimals: int = 2) -> str:
    """Schwab already returns percentage values — just suffix ``%``."""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _money(v: Any) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n >= 1e12:
        return f"${n / 1e12:,.2f}T"
    if n >= 1e9:
        return f"${n / 1e9:,.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:,.2f}M"
    return f"${n:,.2f}"


def _int(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,d}"
    except (TypeError, ValueError):
        return "—"


# Sections ordered for human-mode readability: price context → valuation →
# profitability → balance sheet → dividends → ownership.
_SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Price",
        [
            ("Last", "last", "money"),
            ("52W High", "high52", "num"),
            ("52W Low", "low52", "num"),
            ("Beta", "beta", "num"),
        ],
    ),
    (
        "Valuation",
        [
            ("Market Cap", "marketCap", "money"),
            # Two P/E rows: Schwab's forward ``peRatio`` and a derived
            # TTM (``last / epsTTM``). They diverge for any growing
            # company; surfacing both is the only way a downstream
            # reader can pick the right basis without back-solving.
            ("P/E (fwd)", "peRatioForward", "num"),
            ("P/E (TTM)", "peRatioTtm", "num"),
            ("PEG", "pegRatio", "num"),
            ("P/B", "pbRatio", "num"),
            ("EPS (TTM)", "epsTTM", "num"),
            ("EPS Δ (TTM)", "epsChangePercentTTM", "pct"),
            ("Rev Δ (TTM)", "revChangeTTM", "pct"),
        ],
    ),
    (
        "Profitability",
        [
            ("Gross Margin", "grossMarginTTM", "pct"),
            ("Op Margin", "operatingMarginTTM", "pct"),
            ("Net Margin", "netProfitMarginTTM", "pct"),
            ("ROE", "returnOnEquity", "pct"),
        ],
    ),
    (
        "Balance Sheet",
        [
            ("Current Ratio", "currentRatio", "num"),
            ("Debt/Equity", "totalDebtToEquity", "pct"),
        ],
    ),
    (
        "Dividends",
        [
            ("Yield", "dividendYield", "pct"),
            ("Amount (annual)", "dividendAmount", "num"),
        ],
    ),
    (
        "Ownership",
        [
            ("Shares Out", "sharesOutstanding", "int"),
        ],
    ),
]


def _format_value(kind: str, value: Any) -> str:
    if kind == "money":
        return _money(value)
    if kind == "pct":
        return _pct(value)
    if kind == "int":
        return _int(value)
    return _num(value)


def _to_float(v: Any) -> float | None:
    """Coerce Schwab numeric fields to ``float`` or ``None``.

    Schwab returns numbers but the JSON occasionally has trailing-zero
    strings ("0.0"); be tolerant. Empty / missing / non-numeric → None.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compute_valuation(last: Any, fundamental: dict | None) -> dict | None:
    """Surface forward and TTM P/E side-by-side.

    Returns ``None`` when there's no fundamental block at all so the
    JSON output stays sparse for invalid / non-equity symbols.
    """
    if not fundamental:
        return None
    pe_forward = _to_float(fundamental.get("peRatio"))
    eps_ttm = _to_float(fundamental.get("epsTTM"))
    last_f = _to_float(last)
    pe_ttm: float | None = None
    if last_f is not None and eps_ttm is not None and eps_ttm != 0:
        pe_ttm = last_f / eps_ttm
    return {
        "pe_forward": pe_forward,
        "pe_ttm": pe_ttm,
        "eps_ttm": eps_ttm,
    }


def _data_quality_warnings(symbol: str, fundamental: dict | None) -> list[str]:
    """Detect upstream data oddities worth flagging without mutating values.

    Heuristic (deliberately conservative): symbol contains ``/`` (i.e.
    is a class-share form like ``BRK/B``) AND ``epsTTM > 1000`` (no
    sane B-share has a five-figure EPS — that's the A-share's number
    bleeding through Schwab's response).
    """
    if not fundamental:
        return []
    warnings: list[str] = []
    if "/" in symbol:
        eps = _to_float(fundamental.get("epsTTM"))
        if eps is not None and eps > 1000:
            warnings.append(
                "possible dual-class EPS leak — epsTTM and peRatio likely "
                "reflect the sister share class; verify against issuer filings"
            )
    return warnings


def _shape_row(symbol: str, payload: dict, invalid: set[str]) -> dict:
    if symbol in invalid:
        return {"symbol": symbol, "last": None, "fundamental": None, "error": "invalid symbol"}
    entry = payload.get(symbol) or {}
    quote = entry.get("quote") or {}
    fundamental = entry.get("fundamental") or None
    last = quote.get("lastPrice")
    return {
        "symbol": symbol,
        "last": last,
        "fundamental": fundamental,
        "valuation": _compute_valuation(last, fundamental),
        "data_quality_warnings": _data_quality_warnings(symbol, fundamental),
    }


def render_fundamentals(symbols: list[str], payload: dict, fmt: Format) -> str:
    invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
    rows = [_shape_row(s, payload, invalid) for s in symbols]

    if fmt is Format.JSON:
        return _json.dumps(rows, indent=2, default=str)
    if fmt is Format.MD:
        return _md(rows)
    return _human(rows)


def _human(rows: list[dict]) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=80)
    for i, row in enumerate(rows):
        if i:
            console.print("")
        if row.get("error"):
            console.print(f"[bold]{row['symbol']}[/]  ({row['error']})")
            continue
        header = f"[bold]{row['symbol']}[/]"
        if row.get("last") is not None:
            header += f"  [cyan]{_money(row['last'])}[/]"
        console.print(header)
        for warning in row.get("data_quality_warnings") or []:
            console.print(f"[yellow]⚠ data quality: {warning}[/]")
        console.print("[dim]" + "─" * 60 + "[/]")
        fundamental = row.get("fundamental") or {}
        valuation = row.get("valuation") or {}
        t = Table(show_header=False, box=None, padding=(0, 1), expand=False)
        t.add_column("Metric", width=18)
        t.add_column("Value", justify="right")
        # Price.last isn't in the fundamental block — stitch it in so the
        # first section stays complete even when marketCap etc. are absent.
        # ``peRatioForward`` / ``peRatioTtm`` are derived (see
        # ``_compute_valuation``) so the section table can address them
        # by key without special-casing.
        data = {
            "last": row.get("last"),
            "peRatioForward": valuation.get("pe_forward"),
            "peRatioTtm": valuation.get("pe_ttm"),
            **fundamental,
        }
        for section, metrics in _SECTIONS:
            t.add_row(f"[bold dim]{section}[/]", "")
            for label, key, kind in metrics:
                t.add_row(label, _format_value(kind, data.get(key)))
        console.print(t)
    return buf.getvalue()


def _md(rows: list[dict]) -> str:
    header = (
        "| Symbol | Last | Market Cap | P/E (fwd) | P/E (TTM) | PEG | "
        "EPS (TTM) | EPS Δ (TTM) | Rev Δ (TTM) | Div Yield | Beta | "
        "52W High | 52W Low |"
    )
    sep = (
        "|--------|-----:|-----------:|----------:|----------:|----:|"
        "----------:|------------:|------------:|----------:|-----:|"
        "---------:|--------:|"
    )
    out = [header, sep]
    for row in rows:
        if row.get("error"):
            out.append(
                f"| {row['symbol']} | — | — | — | — | — | — | — | — | — | — | — | — |"
                f"  <!-- {row['error']} -->"
            )
            continue
        f = row.get("fundamental") or {}
        v = row.get("valuation") or {}
        warnings = row.get("data_quality_warnings") or []
        symbol_cell = row["symbol"]
        if warnings:
            symbol_cell = f"{symbol_cell} ⚠"
        line = (
            f"| {symbol_cell} "
            f"| {_money(row.get('last'))} "
            f"| {_money(f.get('marketCap'))} "
            f"| {_num(v.get('pe_forward'))} "
            f"| {_num(v.get('pe_ttm'))} "
            f"| {_num(f.get('pegRatio'))} "
            f"| {_num(f.get('epsTTM'))} "
            f"| {_pct(f.get('epsChangePercentTTM'))} "
            f"| {_pct(f.get('revChangeTTM'))} "
            f"| {_pct(f.get('dividendYield'))} "
            f"| {_num(f.get('beta'))} "
            f"| {_num(f.get('high52'))} "
            f"| {_num(f.get('low52'))} |"
        )
        if warnings:
            line += "  <!-- " + "; ".join(warnings) + " -->"
        out.append(line)
    return "\n".join(out) + "\n"
