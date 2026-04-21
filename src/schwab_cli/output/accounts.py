from __future__ import annotations

import json as _json
from io import StringIO

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _shape_account(raw: dict) -> dict:
    sec = raw.get("securitiesAccount", {})
    bal = sec.get("currentBalances", {}) or {}
    return {
        "accountNumber": sec.get("accountNumber", ""),
        "type": sec.get("type", ""),
        "liquidationValue": bal.get("liquidationValue"),
        "cashBalance": bal.get("cashBalance"),
        "positionCount": len(sec.get("positions") or []),
    }


def _mask_account(n: str) -> str:
    return f"...{n[-4:]}" if len(n) >= 4 else n


def _fmt_money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def render_accounts(raw_list: list[dict], fmt: Format) -> str:
    rows = [_shape_account(a) for a in raw_list]
    if fmt is Format.JSON:
        return _json.dumps(rows, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Account | Type | Liquidation Value | Cash Balance | Positions |",
            "|---------|------|-------------------|--------------|-----------|",
        ]
        for r in rows:
            lines.append(
                f"| {_mask_account(r['accountNumber'])} | {r['type']} | "
                f"{_fmt_money(r['liquidationValue'])} | "
                f"{_fmt_money(r['cashBalance'])} | {r['positionCount']} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title="Accounts")
    t.add_column("Account", style="bold")
    t.add_column("Type")
    t.add_column("Liquidation Value", justify="right")
    t.add_column("Cash Balance", justify="right")
    t.add_column("Positions", justify="right")
    for r in rows:
        t.add_row(
            _mask_account(r["accountNumber"]),
            r["type"],
            _fmt_money(r["liquidationValue"]),
            _fmt_money(r["cashBalance"]),
            str(r["positionCount"]),
        )
    console.print(t)
    return buf.getvalue()


def render_account(raw: dict, fmt: Format) -> str:
    sec = raw.get("securitiesAccount", {})
    data = {
        "accountNumber": sec.get("accountNumber", ""),
        "type": sec.get("type", ""),
        "currentBalances": sec.get("currentBalances", {}),
        "initialBalances": sec.get("initialBalances", {}),
        "positionCount": len(sec.get("positions") or []),
    }
    if fmt is Format.JSON:
        return _json.dumps(data, indent=2)
    if fmt is Format.MD:
        bal = data["currentBalances"] or {}
        lines = [
            f"# Account {_mask_account(data['accountNumber'])}",
            "",
            f"- **Number:** {data['accountNumber']}",
            f"- **Type:** {data['type']}",
            f"- **Liquidation Value:** {_fmt_money(bal.get('liquidationValue'))}",
            f"- **Cash Balance:** {_fmt_money(bal.get('cashBalance'))}",
            f"- **Buying Power:** {_fmt_money(bal.get('buyingPower'))}",
            f"- **Positions:** {data['positionCount']}",
        ]
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title=f"Account {_mask_account(data['accountNumber'])}")
    t.add_column("Field", style="bold")
    t.add_column("Value", justify="right")
    bal = data["currentBalances"] or {}
    t.add_row("Number", data["accountNumber"])
    t.add_row("Type", data["type"])
    t.add_row("Liquidation Value", _fmt_money(bal.get("liquidationValue")))
    t.add_row("Cash Balance", _fmt_money(bal.get("cashBalance")))
    t.add_row("Buying Power", _fmt_money(bal.get("buyingPower")))
    t.add_row("Positions", str(data["positionCount"]))
    console.print(t)
    return buf.getvalue()


def _shape_position(raw: dict) -> dict:
    inst = raw.get("instrument", {}) or {}
    return {
        "account": raw.get("_account", ""),
        "symbol": inst.get("symbol", ""),
        "qty": raw.get("longQuantity") or raw.get("shortQuantity") or 0.0,
        "avgPrice": raw.get("averagePrice"),
        "marketValue": raw.get("marketValue"),
        "dayPnL": raw.get("currentDayProfitLoss"),
        "totalPnL": raw.get("longOpenProfitLoss") or raw.get("shortOpenProfitLoss"),
    }


def render_positions(rows: list[dict], fmt: Format) -> str:
    shaped = [_shape_position(r) for r in rows]
    if fmt is Format.JSON:
        return _json.dumps(shaped, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Account | Symbol | Qty | Avg Price | Market Value | Day P&L | Total P&L |",
            "|---------|--------|-----|-----------|--------------|---------|-----------|",
        ]
        for r in shaped:
            lines.append(
                f"| {_mask_account(r['account'])} | {r['symbol']} | {r['qty']} | "
                f"{_fmt_money(r['avgPrice'])} | {_fmt_money(r['marketValue'])} | "
                f"{_fmt_money(r['dayPnL'])} | {_fmt_money(r['totalPnL'])} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=120)
    t = Table(title="Positions")
    t.add_column("Account")
    t.add_column("Symbol", style="bold")
    t.add_column("Qty", justify="right")
    t.add_column("Avg Price", justify="right")
    t.add_column("Market Value", justify="right")
    t.add_column("Day P&L", justify="right")
    t.add_column("Total P&L", justify="right")
    for r in shaped:
        t.add_row(
            _mask_account(r["account"]),
            r["symbol"],
            f"{r['qty']}",
            _fmt_money(r["avgPrice"]),
            _fmt_money(r["marketValue"]),
            _fmt_money(r["dayPnL"]),
            _fmt_money(r["totalPnL"]),
        )
    console.print(t)
    return buf.getvalue()
