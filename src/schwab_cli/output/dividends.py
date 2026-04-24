"""Renderers for the ``dividends`` command.

Pulls dividend-relevant fields out of the ``fundamental`` block returned
by Schwab's quotes endpoint. Schwab reports ``dividendYield`` as a
percentage value (``0.44`` means 0.44%), so we surface it with a ``%``
suffix — do not multiply by 100 again.

The API exposes only the most-recent + next upcoming dividend event per
symbol. There is no historical series available retroactively; ``vol``-
style local accumulation would be needed for that.

``upcoming_within_days`` filters symbols by ``nextDividendDate``: rows
whose next ex-date is missing, in the past, or further out than the
window are omitted from the rendered output.
"""

from __future__ import annotations

import json as _json
from datetime import date, datetime
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _today() -> date:
    """Hook for tests to freeze 'today' — monkeypatched in the test suite."""
    return date.today()


def _parse_iso_day(s: Any) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s
    text = str(s).strip()
    if not text:
        return None
    # Schwab sends values like '2025-05-12 04:00:00.0' — split on space,
    # strip the sub-second suffix, then parse the leading YYYY-MM-DD.
    first = text.split()[0]
    try:
        return datetime.strptime(first, "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v: Any, decimals: int = 2) -> str:
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
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _date(v: Any) -> str:
    d = _parse_iso_day(v)
    return d.isoformat() if d else "—"


def _freq_label(freq: Any) -> str:
    """Map Schwab's dividendFreq integer to a human label."""
    try:
        n = int(freq)
    except (TypeError, ValueError):
        return "—"
    return {
        0: "none",
        1: "annual",
        2: "semi-annual",
        4: "quarterly",
        6: "bi-monthly",
        12: "monthly",
    }.get(n, f"{n}/yr")


def _is_dividend_payer(fundamental: dict) -> bool:
    """A payer has a non-zero dividendAmount or a future ex-date."""
    amount = fundamental.get("dividendAmount")
    try:
        if amount is not None and float(amount) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return _parse_iso_day(fundamental.get("nextDividendDate")) is not None


def _shape_row(symbol: str, payload: dict, invalid: set[str]) -> dict:
    if symbol in invalid:
        return {"symbol": symbol, "error": "invalid symbol"}
    entry = payload.get(symbol) or {}
    quote = entry.get("quote") or {}
    fundamental = entry.get("fundamental") or {}
    return {
        "symbol": symbol,
        "last": quote.get("lastPrice"),
        "amount_annual": fundamental.get("dividendAmount"),
        "yield_pct": fundamental.get("dividendYield"),
        "frequency_per_year": fundamental.get("dividendFreq"),
        "pay_amount": fundamental.get("dividendPayAmount"),
        "last_ex_date": _date(fundamental.get("dividendDate")),
        "last_pay_date": _date(fundamental.get("dividendPayDate")),
        "declaration_date": _date(fundamental.get("declarationDate")),
        "next_ex_date": _date(fundamental.get("nextDividendDate")),
        "next_pay_date": _date(fundamental.get("nextDividendPayDate")),
        "growth_rate_3y_pct": fundamental.get("divGrowthRate3Year"),
        "is_payer": _is_dividend_payer(fundamental),
        "_raw_next_ex": _parse_iso_day(fundamental.get("nextDividendDate")),
    }


def _apply_upcoming_filter(
    rows: list[dict], upcoming_within_days: int | None
) -> list[dict]:
    if upcoming_within_days is None:
        return rows
    today = _today()
    kept = []
    for row in rows:
        if row.get("error"):
            # Errored rows are dropped under --upcoming because they have no
            # date to filter on.
            continue
        ex = row.get("_raw_next_ex")
        if ex is None:
            continue
        delta = (ex - today).days
        if 0 <= delta <= upcoming_within_days:
            kept.append(row)
    return kept


def render_dividends(
    symbols: list[str],
    payload: dict,
    fmt: Format,
    *,
    upcoming_within_days: int | None = None,
) -> str:
    invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
    rows = [_shape_row(s, payload, invalid) for s in symbols]
    rows = _apply_upcoming_filter(rows, upcoming_within_days)

    if fmt is Format.JSON:
        serializable = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in rows
        ]
        return _json.dumps(serializable, indent=2, default=str)
    if fmt is Format.MD:
        return _md(rows)
    return _human(rows)


def _human(rows: list[dict]) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=80)
    if not rows:
        console.print("(no matching symbols)")
        return buf.getvalue()
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
        console.print("[dim]" + "─" * 60 + "[/]")
        if not row.get("is_payer"):
            console.print("[dim]No dividend (non-payer or API reports none).[/]")
            continue
        t = Table(show_header=False, box=None, padding=(0, 1), expand=False)
        t.add_column("Metric", width=18)
        t.add_column("Value", justify="right")
        t.add_row("Yield", _pct(row.get("yield_pct")))
        t.add_row("Annual amount", _money(row.get("amount_annual")))
        t.add_row("Frequency", _freq_label(row.get("frequency_per_year")))
        t.add_row("Pay amount", _money(row.get("pay_amount")))
        t.add_row("3yr growth", _pct(row.get("growth_rate_3y_pct")))
        t.add_row("[dim]Last ex-date[/]", row.get("last_ex_date") or "—")
        t.add_row("[dim]Last pay date[/]", row.get("last_pay_date") or "—")
        t.add_row("[dim]Next ex-date[/]", row.get("next_ex_date") or "—")
        t.add_row("[dim]Next pay date[/]", row.get("next_pay_date") or "—")
        t.add_row("[dim]Declared[/]", row.get("declaration_date") or "—")
        console.print(t)
    return buf.getvalue()


def _md(rows: list[dict]) -> str:
    header = (
        "| Symbol | Yield | Annual | Pay | Freq | Next ex-date | "
        "Next pay date | Last ex-date | 3yr growth |"
    )
    sep = (
        "|--------|------:|-------:|----:|------|-------------:|"
        "--------------:|-------------:|-----------:|"
    )
    out = [header, sep]
    if not rows:
        out.append("| _(no matching symbols)_ | | | | | | | | |")
        return "\n".join(out) + "\n"
    for row in rows:
        if row.get("error"):
            out.append(
                f"| {row['symbol']} | — | — | — | — | — | — | — | — |"
                f"  <!-- {row['error']} -->"
            )
            continue
        if not row.get("is_payer"):
            out.append(
                f"| {row['symbol']} | — | — | — | none | — | — | — | — |"
            )
            continue
        out.append(
            f"| {row['symbol']} "
            f"| {_pct(row.get('yield_pct'))} "
            f"| {_money(row.get('amount_annual'))} "
            f"| {_money(row.get('pay_amount'))} "
            f"| {_freq_label(row.get('frequency_per_year'))} "
            f"| {row.get('next_ex_date') or '—'} "
            f"| {row.get('next_pay_date') or '—'} "
            f"| {row.get('last_ex_date') or '—'} "
            f"| {_pct(row.get('growth_rate_3y_pct'))} |"
        )
    return "\n".join(out) + "\n"
