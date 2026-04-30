"""Renderers for ``fundamentals`` command.

The Schwab ``/quotes?fields=all`` endpoint returns a per-symbol
``fundamental`` block. Field names verified against the live API:
``eps``, ``peRatio``, ``divAmount``, ``divYield``, ``divFreq``,
``divPayAmount``, ``divExDate``, ``divPayDate``, ``nextDivExDate``,
``nextDivPayDate``, ``declarationDate``, ``sharesOutstanding``,
``avg10DaysVolume``, ``avg1YearVolume``, ``lastEarningsDate``,
``fundLeverageFactor``. (The longer ``epsTTM`` / ``dividendYield``
forms appear in older Schwab docs but are NOT what the live endpoint
returns — using them silently produces ``null`` rows.)

Schwab reports ``divYield`` as a **percentage value** (e.g. ``0.44``
for 0.44%). We surface percentages with a ``%`` suffix — do not
multiply by 100 again.

Human mode stacks a metric/value table per symbol. MD mode renders one
row per symbol. JSON surfaces the raw fundamental block plus the
derived ``valuation`` section and any ``data_quality_warnings``.

P/E semantics
-------------
Schwab's ``peRatio`` is **forward / normalized**; ``eps`` is **TTM**
(trailing 12 months). They use different EPS basis, so for any growing
company ``last / eps != peRatio``. To keep downstream consumers from
confusing the two we surface a derived ``valuation`` section::

    valuation = {
        "pe_forward": fundamental.peRatio,             # Schwab's forward
        "pe_ttm":     last / fundamental.eps,          # derived TTM
        "eps_ttm":    fundamental.eps,
    }

Data quality
------------
Dual-class tickers (BRK.A/B, BF.A/B, GOOG/GOOGL, UA/UAA, …) are
sometimes served with the sister-class EPS leaked into the response
(Schwab upstream bug). We detect this with three independent
signals — known-dual-class membership, EPS too large, P/E too small —
and emit a structured ``data_quality_warnings`` entry::

    {"code": "POSSIBLE_DUAL_CLASS_LEAK",
     "message": "EPS=46563.02 P/E=0.01 likely contaminated by sister
                share class",
     "guidance": "Cross-check public sources (e.g. divide EPS by
                  share-class ratio); known dual-class siblings often
                  trade at a fixed economic ratio (BRK A:B = 1500:1)"}

We do NOT mutate the numbers — Schwab may fix the bug at any time and
a hard-coded ratio would silently corrupt correct data the day after.
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
            ("EPS (TTM)", "eps", "num"),
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
            ("Yield", "divYield", "pct"),
            ("Amount (annual)", "divAmount", "num"),
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

    Reads ``fundamental.eps`` (Schwab's TTM EPS) and ``peRatio``
    (Schwab's forward / normalized P/E). Returns ``None`` when there's
    no fundamental block at all so the JSON output stays sparse for
    invalid / non-equity symbols.
    """
    if not fundamental:
        return None
    pe_forward = _to_float(fundamental.get("peRatio"))
    eps_ttm = _to_float(fundamental.get("eps"))
    last_f = _to_float(last)
    pe_ttm: float | None = None
    if last_f is not None and eps_ttm is not None and eps_ttm > 0:
        pe_ttm = round(last_f / eps_ttm, 4)
    return {
        "pe_forward": pe_forward,
        "pe_ttm": pe_ttm,
        "eps_ttm": eps_ttm,
    }


# Known dual-class equities. Symbols *without* a ``/`` (e.g. ``UA`` /
# ``UAA``, ``GOOG`` / ``GOOGL``) need explicit listing because the
# numeric heuristics alone won't catch a smearing event on those — the
# leaked EPS could be from any class. Membership doesn't imply the data
# is wrong, only that downstream consumers should cross-check.
_DUAL_CLASS_TICKERS: frozenset[str] = frozenset({
    "BRK/A", "BRK/B",
    "BF/A", "BF/B",
    "GOOG", "GOOGL",
    "UA", "UAA",
    "LEN", "LEN/B",
    "MOG/A", "MOG/B",
    "HEI", "HEI/A",
    "FOX", "FOXA",
    "NWS", "NWSA",
    "DISCA", "DISCB", "DISCK",
    "VIAC", "VIACA",
})

# Numeric anomaly thresholds. These deliberately err on the side of
# false negatives: any normal equity has EPS well under $1000 and P/E
# well over 0.1, so anything outside that range is almost certainly
# data corruption rather than a legitimate edge case.
_ANOMALY_EPS_TOO_LARGE = 1000.0
_ANOMALY_PE_TOO_SMALL = 0.1


def _data_quality_warnings(symbol: str, fundamental: dict | None) -> list[dict]:
    """Detect upstream data oddities. Structured (non-string) entries
    so downstream agents can pattern-match on ``code`` rather than
    parse the human message.

    Three independent triggers, OR'd together (any one fires):

    1. Symbol is in the known dual-class set — even if EPS / P/E look
       fine, a future smearing event would go silently undetected
       without this safety net.
    2. ``eps`` exceeds ``_ANOMALY_EPS_TOO_LARGE`` ($1000). No
       legitimate non-bankrupt B-share equity has that EPS.
    3. ``peRatio`` is under ``_ANOMALY_PE_TOO_SMALL`` (0.1) and
       positive. A P/E that low means the EPS basis is wrong.
    """
    if not fundamental:
        return []
    eps = _to_float(fundamental.get("eps"))
    pe = _to_float(fundamental.get("peRatio"))
    triggers: list[str] = []
    if symbol in _DUAL_CLASS_TICKERS:
        triggers.append("known dual-class symbol")
    if eps is not None and eps > _ANOMALY_EPS_TOO_LARGE:
        triggers.append(f"EPS={eps:,.2f} exceeds anomaly threshold "
                        f"(${_ANOMALY_EPS_TOO_LARGE:,.0f})")
    if pe is not None and 0 < pe < _ANOMALY_PE_TOO_SMALL:
        triggers.append(f"P/E={pe:.4f} below anomaly threshold "
                        f"({_ANOMALY_PE_TOO_SMALL})")
    if not triggers:
        return []
    return [{
        "code": "POSSIBLE_DUAL_CLASS_LEAK",
        "message": (
            f"EPS={eps if eps is not None else '—'} "
            f"P/E={pe if pe is not None else '—'} "
            f"likely contaminated by sister share class "
            f"({'; '.join(triggers)})"
        ),
        "guidance": (
            "Cross-check public sources before relying on EPS / P/E. "
            "Known dual-class siblings often trade at a fixed economic "
            "ratio (e.g. BRK A:B = 1500:1) — divide the leaked EPS by "
            "that ratio for an order-of-magnitude check."
        ),
    }]


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
            # Structured warning: render code + message; keep guidance on
            # a dim follow-up line so the primary alert stays scannable.
            console.print(
                f"[yellow]⚠ {warning['code']}: {warning['message']}[/]"
            )
            if warning.get("guidance"):
                console.print(f"[dim]  → {warning['guidance']}[/]")
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
            f"| {_num(f.get('eps'))} "
            f"| {_pct(f.get('epsChangePercentTTM'))} "
            f"| {_pct(f.get('revChangeTTM'))} "
            f"| {_pct(f.get('divYield'))} "
            f"| {_num(f.get('beta'))} "
            f"| {_num(f.get('high52'))} "
            f"| {_num(f.get('low52'))} |"
        )
        if warnings:
            line += "  <!-- " + "; ".join(
                f"{w['code']}: {w['message']}" for w in warnings
            ) + " -->"
        out.append(line)
    return "\n".join(out) + "\n"
