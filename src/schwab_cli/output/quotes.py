from __future__ import annotations

import json as _json
from io import StringIO

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _fmt_num(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _shape_row(symbol: str, payload: dict, invalid: set[str]) -> dict:
    if symbol in invalid:
        return {
            "symbol": symbol,
            "last": None,
            "change": None,
            "changePct": None,
            "bid": None,
            "ask": None,
            "volume": None,
            "error": "invalid symbol",
        }
    entry = payload.get(symbol) or {}
    q = entry.get("quote") or {}
    return {
        "symbol": symbol,
        "last": q.get("lastPrice"),
        "change": q.get("netChange"),
        "changePct": q.get("netPercentChangeInDouble") or q.get("netPercentChange"),
        "bid": q.get("bidPrice"),
        "ask": q.get("askPrice"),
        "volume": q.get("totalVolume"),
    }


def render_quotes(symbols: list[str], payload: dict, fmt: Format) -> str:
    invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
    rows = [_shape_row(s, payload, invalid) for s in symbols]

    if fmt is Format.JSON:
        return _json.dumps(rows, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Symbol | Last | Change | Change% | Bid | Ask | Volume |",
            "|--------|------|--------|---------|-----|-----|--------|",
        ]
        for r in rows:
            lines.append(
                f"| {r['symbol']} | {_fmt_num(r['last'])} | {_fmt_num(r['change'])} | "
                f"{_fmt_num(r['changePct'])} | {_fmt_num(r['bid'])} | "
                f"{_fmt_num(r['ask'])} | {_fmt_num(r['volume'], 0)} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title="Quotes")
    t.add_column("Symbol", style="bold")
    t.add_column("Last", justify="right")
    t.add_column("Change", justify="right")
    t.add_column("Change%", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Volume", justify="right")
    for r in rows:
        t.add_row(
            r["symbol"],
            _fmt_num(r["last"]),
            _fmt_num(r["change"]),
            _fmt_num(r["changePct"]),
            _fmt_num(r["bid"]),
            _fmt_num(r["ask"]),
            _fmt_num(r["volume"], 0),
        )
    console.print(t)
    return buf.getvalue()
