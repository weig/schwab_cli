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
Detection is **evidence-based**, not symbol-based. Some dual-class
tickers (BRK.A/B at a 1500:1 ratio) are served with the sister-class
EPS leaked into the response — a Schwab upstream bug. Other dual-class
tickers (GOOG/GOOGL, 1:1 economics) are NOT affected — the data is
returned correctly per ticker. So the heuristic looks at the numbers,
not the symbol::

    1. abs(eps) > 1000              # no real equity has EPS this large
    2. 0 < peRatio < 1.0            # a positive P/E this small ⇒ EPS basis wrong
    3. last > 0 and |eps/last| < 0.001  # EPS this tiny relative to price ⇒ P/E > 1000

Any one of those firing emits a structured ``data_quality_warnings``
entry. The ``code`` distinguishes class-share suspects from generic
anomalies — ``POSSIBLE_DUAL_CLASS_LEAK`` when the symbol contains
``/`` (the corruption is most plausibly sister-class smearing),
``ANOMALOUS_FUNDAMENTALS`` otherwise. Downstream agents pattern-match
on ``code`` rather than parsing free text::

    {"code": "POSSIBLE_DUAL_CLASS_LEAK",
     "message": "EPS=46563.02 P/E=0.01 likely contaminated by sister
                share class (abs(EPS) > 1000; P/E < 1.0)",
     "guidance": "Cross-check public sources..."}

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
from schwab_cli.service.types import FundamentalsResult


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


# Numeric anomaly thresholds. These deliberately err on the side of
# false negatives — every healthy equity sits well inside these bounds,
# so anything outside is almost certainly data corruption rather than a
# legitimate edge case.
#
# Calibrated against real Schwab responses:
# - GOOG (eps=10.80, pe=32.17, last=372): clean, must NOT fire
# - TSLA (eps=1.08, pe=345.20, last=345): clean (high P/E is real, not noise)
# - BRK/B leak (eps=46563, pe=0.01, last=474): MUST fire on all three
_ANOMALY_EPS_ABS_TOO_LARGE = 1000.0
_ANOMALY_PE_TOO_SMALL = 1.0
_ANOMALY_EPS_TO_LAST_RATIO_FLOOR = 0.001


def _contamination_triggers(
    eps: float | None, pe: float | None, last: float | None
) -> list[str]:
    """Return human-readable trigger descriptions for any signals that
    fired. Empty list ⇒ data passes inspection.

    Three independent signals; any one is sufficient:

    1. ``abs(eps) > _ANOMALY_EPS_ABS_TOO_LARGE``. No real equity has
       EPS in this range (BRK.A is the canonical exception, and it
       leaks into BRK.B as a five-figure number).
    2. ``0 < pe < _ANOMALY_PE_TOO_SMALL``. A positive P/E under 1.0
       means the EPS basis used to compute it is wrong.
    3. ``last > 0 and abs(eps/last) < _ANOMALY_EPS_TO_LAST_RATIO_FLOOR``.
       EPS this small relative to price implies P/E > 1000 (the
       inverse failure mode of #2).
    """
    triggers: list[str] = []
    if eps is not None and abs(eps) > _ANOMALY_EPS_ABS_TOO_LARGE:
        triggers.append(
            f"abs(EPS)={abs(eps):,.2f} > ${_ANOMALY_EPS_ABS_TOO_LARGE:,.0f}"
        )
    if pe is not None and 0 < pe < _ANOMALY_PE_TOO_SMALL:
        triggers.append(f"P/E={pe:.4f} < {_ANOMALY_PE_TOO_SMALL}")
    if (
        eps is not None
        and last is not None
        and last > 0
        and abs(eps / last) < _ANOMALY_EPS_TO_LAST_RATIO_FLOOR
    ):
        triggers.append(
            f"|EPS/last|={abs(eps / last):.5f} < "
            f"{_ANOMALY_EPS_TO_LAST_RATIO_FLOOR}"
        )
    return triggers


def _data_quality_warnings(
    symbol: str, fundamental: dict | None, last: Any
) -> list[dict]:
    """Detect upstream data oddities. Structured (non-string) entries
    so downstream agents can pattern-match on ``code`` rather than
    parse the human message.

    Returns at most one warning per row. The ``code`` reflects whether
    the symbol *could* plausibly be a dual-class leak target (contains
    ``/``) or is just a generic anomaly on a single-class ticker.
    """
    if not fundamental:
        return []
    eps = _to_float(fundamental.get("eps"))
    pe = _to_float(fundamental.get("peRatio"))
    last_f = _to_float(last)
    triggers = _contamination_triggers(eps, pe, last_f)
    if not triggers:
        return []
    is_class_share = "/" in symbol
    eps_str = f"{eps:.4f}" if eps is not None else "—"
    pe_str = f"{pe:.4f}" if pe is not None else "—"
    if is_class_share:
        return [{
            "code": "POSSIBLE_DUAL_CLASS_LEAK",
            "message": (
                f"EPS={eps_str} P/E={pe_str} likely contaminated by "
                f"sister share class ({'; '.join(triggers)})"
            ),
            "guidance": (
                "Cross-check public sources before relying on EPS / P/E. "
                "Class-share siblings often trade at a fixed economic "
                "ratio (e.g. BRK A:B = 1500:1) — divide the leaked EPS "
                "by that ratio for an order-of-magnitude check."
            ),
        }]
    return [{
        "code": "ANOMALOUS_FUNDAMENTALS",
        "message": (
            f"EPS={eps_str} P/E={pe_str} look anomalous "
            f"({'; '.join(triggers)}); data quality unverified"
        ),
        "guidance": (
            "Cross-check public sources before relying on EPS / P/E. "
            "Schwab's fundamental block has been observed to return "
            "stale or mis-mapped values for some symbols."
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
        "data_quality_warnings": _data_quality_warnings(symbol, fundamental, last),
    }


def render_fundamentals_result(result: FundamentalsResult, fmt: Format) -> str:
    """Render a :class:`FundamentalsResult` to text, byte-identical to the
    legacy :func:`render_fundamentals` output for the same data."""
    return render_fundamentals(list(result.symbols), dict(result.payload), fmt)


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
