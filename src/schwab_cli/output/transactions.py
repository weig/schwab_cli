from __future__ import annotations

import json as _json
import math
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _finite(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    return fv


def _mask_account(n: str) -> str:
    return f"...{n[-4:]}" if n and len(n) >= 4 else (n or "")


def _main_leg(raw: dict) -> dict | None:
    """Pick the non-fee transfer item as the 'main' economic leg.

    Fee entries have `feeType` set (COMMISSION, SEC_FEE, etc.); the main
    leg has none. Returns None if no non-fee items found.
    """
    items = raw.get("transferItems") or []
    for it in items:
        if it.get("feeType") is None:
            return it
    return None


def _display_symbol(raw: dict, main: dict) -> str:
    """Pick the most informative symbol for display.

    Trades use the instrument symbol directly. Dividends, journals, cash
    receipts, and interest entries carry only a CURRENCY_USD leg with no
    ticker — the actual company/reason sits in the top-level `description`.
    """
    inst = main.get("instrument") or {}
    sym = inst.get("symbol") or ""
    if inst.get("assetType") == "CURRENCY" or sym == "CURRENCY_USD":
        desc = raw.get("description")
        if desc:
            return desc
    return sym


def shape_transactions(raw_list: list[dict]) -> list[dict]:
    """Flatten Schwab transaction payloads into display rows.

    Each row is:
        account, date, time, type, symbol, qty, price, effect, netAmount
    Sorted ascending by `time`.
    """
    shaped: list[dict] = []
    for raw in raw_list or []:
        t = raw.get("time") or ""
        main = _main_leg(raw) or {}
        shaped.append({
            "account": raw.get("_account") or "",
            "date": t[:10],
            "time": t,
            "type": raw.get("type") or "",
            "symbol": _display_symbol(raw, main),
            "qty": _finite(main.get("amount")),
            "price": _finite(main.get("price")),
            "effect": main.get("positionEffect"),
            "netAmount": _finite(raw.get("netAmount")),
        })
    shaped.sort(key=lambda r: r["time"])
    return shaped


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_qty(v: Any) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    # Integer-ish quantities render without decimals; fractional with up to 4.
    if fv == int(fv):
        return f"{int(fv):+,d}" if fv != 0 else "0"
    return f"{fv:+,.4f}"


def _fmt_net_colored(v: Any) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{fv:+,.2f}"
    if fv > 0:
        return f"[green]{s}[/]"
    if fv < 0:
        return f"[red]{s}[/]"
    return s


def _fmt_net_plain(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+,.2f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _render_json(rows: list[dict]) -> str:
    return _json.dumps(rows, indent=2)


# ---------------------------------------------------------------------------
# HUMAN
# ---------------------------------------------------------------------------

def _render_human(
    rows: list[dict],
    *,
    show_account: bool = True,
    cache_stats: dict | None = None,
) -> str:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=140,
    )

    count = len(rows)
    net_total = sum((r["netAmount"] or 0.0) for r in rows)
    header = (
        f"[dim]Transactions — {count} row{'s' if count != 1 else ''}   "
        f"Net cashflow: {_fmt_net_colored(net_total)}[/dim]"
    )
    console.print(header, highlight=False)
    if cache_stats and cache_stats.get("total"):
        # Phrase as two counts — the visible row count may differ from
        # cache_stats["total"] when --type filters cull non-TRADE rows.
        # Showing the pair (cache hits, API fetched) makes clear these
        # are cache-layer events, not the displayed row total.
        from_cache = int(cache_stats.get("from_cache", 0))
        from_api = int(cache_stats.get("from_api", 0))
        console.print(
            f"[dim]Cache: {from_cache} hits, {from_api} fetched from Schwab[/dim]",
            highlight=False,
        )
    console.print("")

    if not rows:
        console.print("[dim](no transactions in range)[/dim]", highlight=False)
        return buf.getvalue()

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("Date")
    if show_account:
        t.add_column("Account")
    t.add_column("Type")
    t.add_column("Symbol")
    t.add_column("Effect")
    t.add_column("Qty", justify="right")
    t.add_column("Price", justify="right")
    t.add_column("Net", justify="right")

    for r in rows:
        cells = [r["date"]]
        if show_account:
            cells.append(_mask_account(r["account"]))
        cells += [
            r["type"],
            r["symbol"] or "—",
            r["effect"] or "—",
            _fmt_qty(r["qty"]),
            _fmt(r["price"]) if r["price"] is not None else "—",
            _fmt_net_colored(r["netAmount"]),
        ]
        t.add_row(*cells)
    console.print(t)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MD
# ---------------------------------------------------------------------------

def _render_md(rows: list[dict], *, show_account: bool = True) -> str:
    count = len(rows)
    net_total = sum((r["netAmount"] or 0.0) for r in rows)
    lines = [
        f"# Transactions — {count} row{'s' if count != 1 else ''}",
        "",
        f"**Net cashflow:** {_fmt_net_plain(net_total)}",
        "",
    ]

    if not rows:
        lines.append("_(no transactions in range)_")
        return "\n".join(lines) + "\n"

    if show_account:
        lines += [
            "| Date | Account | Type | Symbol | Effect | Qty | Price | Net |",
            "|------|---------|------|--------|--------|-----|-------|-----|",
        ]
    else:
        lines += [
            "| Date | Type | Symbol | Effect | Qty | Price | Net |",
            "|------|------|--------|--------|-----|-------|-----|",
        ]
    for r in rows:
        if show_account:
            lines.append(
                "| {date} | {acct} | {type} | {sym} | {eff} | {qty} | {price} | {net} |".format(
                    date=r["date"],
                    acct=_mask_account(r["account"]),
                    type=r["type"],
                    sym=r["symbol"] or "—",
                    eff=r["effect"] or "—",
                    qty=_fmt_qty(r["qty"]),
                    price=_fmt(r["price"]) if r["price"] is not None else "—",
                    net=_fmt_net_plain(r["netAmount"]),
                )
            )
        else:
            lines.append(
                "| {date} | {type} | {sym} | {eff} | {qty} | {price} | {net} |".format(
                    date=r["date"],
                    type=r["type"],
                    sym=r["symbol"] or "—",
                    eff=r["effect"] or "—",
                    qty=_fmt_qty(r["qty"]),
                    price=_fmt(r["price"]) if r["price"] is not None else "—",
                    net=_fmt_net_plain(r["netAmount"]),
                )
            )
    return "\n".join(lines) + "\n"


def render_transactions(
    rows: list[dict],
    *,
    fmt: Format,
    show_account: bool = True,
    cache_stats: dict | None = None,
) -> str:
    """Render shaped transaction rows.

    ``show_account=False`` drops the Account column from human and MD
    output — used when the caller has already filtered to a single
    account, making the column redundant. JSON output is unchanged
    for shape stability.

    ``cache_stats`` (when provided) surfaces the cache hit ratio in
    the human-view header. Format: ``{"total": N, "from_cache": M, ...}``.
    Omitted from JSON / MD — those are for downstream consumers, not
    diagnostics.
    """
    if fmt is Format.JSON:
        return _render_json(rows)
    if fmt is Format.MD:
        return _render_md(rows, show_account=show_account)
    if fmt is Format.HUMAN:
        return _render_human(
            rows, show_account=show_account, cache_stats=cache_stats,
        )
    raise NotImplementedError(f"format {fmt} not yet implemented")
