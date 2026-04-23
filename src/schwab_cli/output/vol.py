"""Render the ``vol`` command envelope.

Envelope shape produced by ``commands/vol.py``::

    {
        "symbol": "NVDA",
        "spot": 202.50,
        "iv": {
            "value": 0.36582,
            "expiry": "2026-05-01",
            "dte": 9,
            "strike": 202.5
        },
        "hv": {
            "window": 30,
            "value": 0.2841
        },
        "hvp": {
            "lookback": 252,
            "value": 68.0,          # percentile 0–100
            "sample_size": 252
        },
        "pc": {
            "volume_ratio": 0.72,
            "oi_ratio": 0.94,
            "call_volume": ..., "put_volume": ...,
            "call_oi": ...,      "put_oi": ...
        },
        "ivp": {
            "state": "not_yet_active",
            "value": None,
            "sample_size": 0,
            "lookback": 252,
            "message": "phase 2: local accumulation not wired up"
        }
    }
"""

from __future__ import annotations

import json as _json
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def render_vol(env: dict[str, Any], *, fmt: Format) -> str:
    if fmt is Format.JSON:
        return _json.dumps(env, indent=2, default=str)
    if fmt is Format.MD:
        return _md(env)
    return _human(env)


# ---- formatters ---------------------------------------------------------


def _pct(v: Any, places: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{places}f}%"


def _percentile(v: Any) -> str:
    """HVP / IVP renderer — integer percentile."""
    if v is None:
        return "—"
    return f"{v:.0f}%"


def _ratio(v: Any) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _money(v: Any) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _ivp_value_and_note(ivp: dict[str, Any]) -> tuple[str, str]:
    """Split the IVP row into (value, note) so wide notes don't wrap values."""
    state = ivp.get("state")
    value = ivp.get("value")
    n = ivp.get("sample_size", 0)
    lookback = ivp.get("lookback", 252)
    if state == "ok":
        return _percentile(value), f"{lookback}-day percentile"
    if state == "partial":
        return _percentile(value), f"partial: {n}/{lookback} days"
    if state == "insufficient":
        return "—", f"insufficient history: {n}/{lookback} days"
    # "not_yet_active" or any unknown state
    return "—", "not yet active (phase 2)"


# ---- HUMAN --------------------------------------------------------------


def _human(env: dict[str, Any]) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=80)

    # Header
    console.print(f"[bold]{env['symbol']}[/]  [cyan]{_money(env['spot'])}[/]")
    console.print("[dim]" + "─" * 60 + "[/]")

    t = Table(show_header=False, box=None, padding=(0, 1), expand=False)
    t.add_column(width=8)              # label
    t.add_column(justify="right", width=10)  # value
    t.add_column(overflow="fold")      # note

    iv = env.get("iv") or {}
    iv_note = ""
    if iv.get("expiry"):
        iv_note = f"ATM {iv['expiry']}, {iv.get('dte', '?')} DTE, strike {_money(iv.get('strike'))}"
    t.add_row("IV", _pct(iv.get("value")), f"[dim]{iv_note}[/]")

    hv = env.get("hv") or {}
    t.add_row("HV", _pct(hv.get("value")), f"[dim]{hv.get('window')}-day realized[/]")

    hvp = env.get("hvp") or {}
    hvp_note = f"{hvp.get('lookback', 252)}-day percentile"
    if hvp.get("sample_size", 0) < hvp.get("lookback", 252):
        hvp_note += f" ({hvp.get('sample_size', 0)}/{hvp.get('lookback', 252)} available)"
    t.add_row("HVP", _percentile(hvp.get("value")), f"[dim]{hvp_note}[/]")

    pc = env.get("pc") or {}
    t.add_row("P/C vol", _ratio(pc.get("volume_ratio")), "[dim]puts/calls, volume, all expiries[/]")
    t.add_row("P/C OI", _ratio(pc.get("oi_ratio")), "[dim]puts/calls, open interest, all expiries[/]")

    ivp = env.get("ivp") or {}
    ivp_val, ivp_note = _ivp_value_and_note(ivp)
    t.add_row("IVP", ivp_val, f"[dim]{ivp_note}[/]")

    console.print(t)
    return buf.getvalue().rstrip("\n") + "\n"


# ---- MD -----------------------------------------------------------------


def _md(env: dict[str, Any]) -> str:
    iv = env.get("iv") or {}
    hv = env.get("hv") or {}
    hvp = env.get("hvp") or {}
    pc = env.get("pc") or {}
    ivp = env.get("ivp") or {}

    lines = []
    lines.append(f"# {env['symbol']} — {_money(env['spot'])}")
    lines.append("")
    lines.append("| Metric | Value | Context |")
    lines.append("| --- | ---: | --- |")
    iv_note = ""
    if iv.get("expiry"):
        iv_note = (
            f"ATM {iv['expiry']}, {iv.get('dte', '?')} DTE, "
            f"strike {_money(iv.get('strike'))}"
        )
    lines.append(f"| IV | {_pct(iv.get('value'))} | {iv_note} |")
    lines.append(f"| HV | {_pct(hv.get('value'))} | {hv.get('window')}-day realized |")
    hvp_note = f"{hvp.get('lookback', 252)}-day percentile"
    if hvp.get("sample_size", 0) < hvp.get("lookback", 252):
        hvp_note += (
            f" ({hvp.get('sample_size', 0)}/{hvp.get('lookback', 252)} available)"
        )
    lines.append(f"| HVP | {_percentile(hvp.get('value'))} | {hvp_note} |")
    lines.append(
        f"| P/C vol | {_ratio(pc.get('volume_ratio'))} | puts/calls, volume, all expiries |"
    )
    lines.append(
        f"| P/C OI | {_ratio(pc.get('oi_ratio'))} | puts/calls, OI, all expiries |"
    )
    ivp_val, ivp_note = _ivp_value_and_note(ivp)
    lines.append(f"| IVP | {ivp_val} | {ivp_note} |")
    lines.append("")
    return "\n".join(lines) + "\n"
