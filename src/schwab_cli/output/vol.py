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
    """Split the IVP row into (value, note) so wide notes don't wrap values.

    Annotations:

    * Below the minimum sample size, the value cell stays ``—`` and the
      note surfaces today's IV against the sample's min/max range so the
      user gets actionable context without a fake-precise percentile.
    * Partial/ok states carry the lookback scope and, if any synthetic
      rows contributed, the synthetic/observed breakdown.
    """
    state = ivp.get("state")
    value = ivp.get("value")
    n = ivp.get("sample_size", 0)
    lookback = ivp.get("lookback", 252)
    synthetic = ivp.get("synthetic", 0)
    observed = ivp.get("observed", 0)
    today_iv = ivp.get("today_iv")
    range_min = ivp.get("range_min")
    range_max = ivp.get("range_max")

    def _breakdown() -> str:
        if synthetic > 0:
            return f"  ({synthetic} synthetic, {observed} observed)"
        return ""

    if state == "ok":
        return _percentile(value), f"{lookback}-day percentile" + _breakdown()
    if state == "partial":
        return _percentile(value), f"partial: {n}/{lookback} days" + _breakdown()
    if state == "insufficient":
        # Fold the useful context (sample range + today's IV) into the
        # note instead of printing a misleading percentile.
        if (
            range_min is not None
            and range_max is not None
            and today_iv is not None
            and n > 0
        ):
            note = (
                f"{n}-day sample too small for percentile; "
                f"today {today_iv * 100:.2f}% vs "
                f"{range_min * 100:.1f}-{range_max * 100:.1f}% range"
                + _breakdown()
            )
        else:
            note = f"insufficient history: {n}/{lookback} days" + _breakdown()
        return "—", note
    return "—", "not yet active"


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

    iv_ref = env.get("iv_ref")
    if iv_ref and iv_ref.get("value") is not None:
        ref_note = (
            f"LEAPS {iv_ref['expiry']}, {iv_ref.get('dte', '?')} DTE, "
            f"strike {_money(iv_ref.get('strike'))} (used for IVP)"
        )
        t.add_row("IV (1y)", _pct(iv_ref.get("value")), f"[dim]{ref_note}[/]")

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
    iv_ref = env.get("iv_ref")
    if iv_ref and iv_ref.get("value") is not None:
        ref_note = (
            f"LEAPS {iv_ref['expiry']}, {iv_ref.get('dte', '?')} DTE, "
            f"strike {_money(iv_ref.get('strike'))} (used for IVP)"
        )
        lines.append(f"| IV (1y) | {_pct(iv_ref.get('value'))} | {ref_note} |")
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


# ---- v3 human renderer --------------------------------------------------


def render_vol_human(snap: dict) -> str:
    """Human-readable v3 vol snapshot — term structure, HV, skew,
    plus IVR/IVP source annotation.

    ``snap`` is the dict returned by the v3 vol command (after the
    3-tier fallback computes ``ivr_ivp``).
    """
    lines = [
        f"{snap['symbol']}  vol snapshot — {snap['as_of']} (NY)",
        f"  Spot:        {snap['spot']:.2f}",
        f"  Front IV:    {snap['atm_iv']*100:.2f}% (DTE {snap.get('atm_dte', '—')})",
    ]
    for tenor in (30, 60, 90):
        v = snap.get(f"atm_iv_{tenor}d")
        if v is not None:
            lines.append(f"  ATM IV {tenor}d:  {v*100:.2f}%")
        else:
            lines.append(f"  ATM IV {tenor}d:  —")
    hv = snap.get("hv_30d")
    if hv is not None:
        lines.append(f"  HV  30d:     {hv*100:.2f}%")
    else:
        lines.append(f"  HV  30d:     —")
    rr = snap.get("ivr_ivp", {})
    if rr.get("ivr") is not None:
        suffix = " ⚠ backfilled" if rr.get("backfilled") else ""
        lines.append(
            f"  IV Rank:     {rr['ivr']:.1f}%        "
            f"(252d, {rr['source']}, {rr['n_days']} days{suffix})"
        )
        lines.append(
            f"  IV %ile:     {rr['ivp']:.1f}%        "
            f"(252d, {rr['source']}, {rr['n_days']} days{suffix})"
        )
    else:
        lines.append("  IV Rank:     — (low history)")
        lines.append("  IV %ile:     — (low history)")
    for tenor in (30, 60, 90):
        p = snap.get(f"iv_25d_put_{tenor}d")
        c = snap.get(f"iv_25d_call_{tenor}d")
        if p is not None and c is not None:
            pts = (p - c) * 100
            sign = "+" if pts >= 0 else "−"
            lines.append(
                f"  Skew  {tenor}d:   {sign}{abs(pts):.1f} vol pts "
                f"(25Δ put − 25Δ call)"
            )
        else:
            lines.append(f"  Skew  {tenor}d:   —")
    return "\n".join(lines)
