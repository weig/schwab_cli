"""Detail view for a single option contract (the `greeks` command).

The chain response for a single strike carries everything we need — the
`_shape_contract` in ``output/chains.py`` already extracts greeks, quote
levels, volume/OI, IV, and value decomposition. We compose that flattened
contract dict + the underlying into a dedicated one-contract view:
compact HUMAN rich tables, raw JSON, or a markdown equivalent.
"""

from __future__ import annotations

import json as _json
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def render_greeks(envelope: dict, *, fmt: Format) -> str:
    """Render the greeks view for a single contract.

    `envelope` is the shape the command produces: the underlying block from
    the chain response plus a single resolved contract.
    """
    if fmt is Format.JSON:
        return _json.dumps(envelope, indent=2, default=str)
    if fmt is Format.MD:
        return _md(envelope)
    return _human(envelope)


# ---- shared helpers -----------------------------------------------------


def _money(v: Any) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"


def _pct_plain(v: Any) -> str:
    """Non-signed percent (for IV, prob-ITM, etc.)."""
    if v is None:
        return "—"
    return f"{v * 100:.2f}%"


def _num(v: Any, places: int = 4) -> str:
    if v is None:
        return "—"
    return f"{v:.{places}f}"


def _int_comma(v: Any) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _be(contract: dict, spot: float | None) -> tuple[float | None, float | None]:
    """Break-even price + percent move from spot.

    call break-even = strike + mark    ;    put = strike - mark
    (Uses mark if present, else mid of bid/ask, else last.)
    """
    mark = contract.get("mark")
    if mark is None:
        bid, ask = contract.get("bid"), contract.get("ask")
        if bid is not None and ask is not None:
            mark = (bid + ask) / 2
    if mark is None:
        mark = contract.get("last")
    strike = contract.get("strike")
    if mark is None or strike is None:
        return None, None
    be = strike + mark if contract["side"] == "C" else strike - mark
    if spot in (None, 0):
        return be, None
    return be, (be - spot) / spot


# ---- HUMAN (rich) -------------------------------------------------------


def _human(env: dict) -> str:
    u = env["underlying"]
    c = env["contract"]
    side_word = "CALL" if c["side"] == "C" else "PUT"
    spot = u.get("last")
    be, be_move = _be(c, spot)

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=100)

    # Header line
    header = (
        f"[bold]{env['underlyingSymbol']}[/] {env['expiry']} "
        f"{side_word} [cyan]${c['strike']:.2f}[/]  "
        f"([dim]{c['optionSymbol']}[/])"
    )
    console.print(header)
    dte = env.get("dte")
    dte_line = f"Expiry {env['expiry']}" + (f" ({dte} DTE)" if dte is not None else "")
    console.print(f"[dim]{dte_line}[/]")
    spot_chg = u.get("netChange")
    spot_pct = u.get("pctChange")
    chg = ""
    if spot_chg is not None and spot_pct is not None:
        color = "green" if spot_chg >= 0 else "red"
        chg = f"  [{color}]{spot_chg:+.2f} / {spot_pct:+.2f}%[/]"
    console.print(f"[dim]Underlying[/]  {_money(spot)}{chg}")
    console.print()

    # Quote + Greeks side-by-side
    t = Table.grid(padding=(0, 3), expand=False)
    t.add_column(justify="left")
    t.add_column(justify="left")

    quote = Table(title="Quote", title_justify="left", show_header=False,
                  box=None, padding=(0, 1))
    quote.add_column(); quote.add_column(justify="right")
    quote.add_row("Bid", _money(c.get("bid")))
    quote.add_row("Ask", _money(c.get("ask")))
    quote.add_row("Mid",
                  _money((c["bid"] + c["ask"]) / 2)
                  if c.get("bid") is not None and c.get("ask") is not None
                  else "—")
    quote.add_row("Last", _money(c.get("last")))
    quote.add_row("Mark", _money(c.get("mark")))
    quote.add_row("Volume", _int_comma(c.get("volume")))
    quote.add_row("Open Int.", _int_comma(c.get("openInterest")))

    greeks = Table(title="Greeks", title_justify="left", show_header=False,
                   box=None, padding=(0, 1))
    greeks.add_column(); greeks.add_column(justify="right")
    greeks.add_row("Δ  delta", _num(c.get("delta")))
    greeks.add_row("Γ  gamma", _num(c.get("gamma")))
    greeks.add_row("Θ  theta", _num(c.get("theta")) + " /day" if c.get("theta") is not None else "—")
    greeks.add_row("𝒱  vega",
                   _num(c.get("vega")) + " /1% IV" if c.get("vega") is not None else "—")
    greeks.add_row("ρ  rho",
                   _num(c.get("rho")) + " /1% rate" if c.get("rho") is not None else "—")
    greeks.add_row("IV", _pct_plain(c.get("iv")))

    t.add_row(quote, greeks)
    console.print(t)
    console.print()

    # Value decomposition
    val = Table(title="Value", title_justify="left", show_header=False,
                box=None, padding=(0, 1))
    val.add_column(); val.add_column(justify="right")
    val.add_row("Intrinsic", _money(c.get("intrinsic")))
    val.add_row("Extrinsic (time)", _money(c.get("timeValue")))
    if be is not None:
        be_str = _money(be)
        if be_move is not None:
            color = "green" if be_move >= 0 else "red"
            be_str += f"  [{color}]({be_move * 100:+.2f}% vs spot)[/]"
        val.add_row("Break-even", be_str)
    val.add_row("In the money", "yes" if c.get("inTheMoney") else "no")
    val.add_row("Multiplier", str(c.get("multiplier") or "—"))
    val.add_row("Settlement", c.get("settlementType") or "—")
    console.print(val)

    return buf.getvalue().rstrip("\n") + "\n"


# ---- MD -----------------------------------------------------------------


def _md(env: dict) -> str:
    u = env["underlying"]
    c = env["contract"]
    side_word = "CALL" if c["side"] == "C" else "PUT"
    spot = u.get("last")
    be, be_move = _be(c, spot)

    lines = []
    lines.append(
        f"# {env['underlyingSymbol']} {env['expiry']} {side_word} "
        f"${c['strike']:.2f}"
    )
    lines.append("")
    lines.append(f"**Contract:** `{c['optionSymbol']}`")
    if env.get("dte") is not None:
        lines.append(f"**Expiry:** {env['expiry']} ({env['dte']} DTE)")
    else:
        lines.append(f"**Expiry:** {env['expiry']}")
    if spot is not None:
        chg = ""
        if u.get("netChange") is not None and u.get("pctChange") is not None:
            chg = f" ({u['netChange']:+.2f} / {u['pctChange']:+.2f}%)"
        lines.append(f"**Underlying:** {_money(spot)}{chg}")
    lines.append("")

    lines.append("## Quote")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | ---: |")
    mid = None
    if c.get("bid") is not None and c.get("ask") is not None:
        mid = (c["bid"] + c["ask"]) / 2
    for label, val in [
        ("Bid", _money(c.get("bid"))),
        ("Ask", _money(c.get("ask"))),
        ("Mid", _money(mid) if mid is not None else "—"),
        ("Last", _money(c.get("last"))),
        ("Mark", _money(c.get("mark"))),
        ("Volume", _int_comma(c.get("volume"))),
        ("Open Interest", _int_comma(c.get("openInterest"))),
    ]:
        lines.append(f"| {label} | {val} |")
    lines.append("")

    lines.append("## Greeks")
    lines.append("")
    lines.append("| Greek | Value |")
    lines.append("| --- | ---: |")
    for label, val in [
        ("Δ delta", _num(c.get("delta"))),
        ("Γ gamma", _num(c.get("gamma"))),
        ("Θ theta (per day)", _num(c.get("theta"))),
        ("𝒱 vega (per 1% IV)", _num(c.get("vega"))),
        ("ρ rho (per 1% rate)", _num(c.get("rho"))),
        ("IV", _pct_plain(c.get("iv"))),
    ]:
        lines.append(f"| {label} | {val} |")
    lines.append("")

    lines.append("## Value")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | ---: |")
    lines.append(f"| Intrinsic | {_money(c.get('intrinsic'))} |")
    lines.append(f"| Extrinsic (time) | {_money(c.get('timeValue'))} |")
    if be is not None:
        be_display = _money(be)
        if be_move is not None:
            be_display += f" ({be_move * 100:+.2f}% vs spot)"
        lines.append(f"| Break-even | {be_display} |")
    lines.append(f"| In the money | {'yes' if c.get('inTheMoney') else 'no'} |")
    lines.append(f"| Multiplier | {c.get('multiplier') or '—'} |")
    lines.append(f"| Settlement | {c.get('settlementType') or '—'} |")
    lines.append("")

    return "\n".join(lines) + "\n"
