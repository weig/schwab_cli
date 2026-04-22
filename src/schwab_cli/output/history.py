from __future__ import annotations

import json as _json
import math
from datetime import datetime
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format

_NY = ZoneInfo("America/New_York")


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


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt_dt(ms: int, *, interval: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=_NY)
    if interval.endswith("min"):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.strftime("%Y-%m-%d")


def _fmt_iso_ny(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=_NY)
    return dt.isoformat()


def _compute_change(close: float | None, prior: float | None) -> tuple[float | None, float | None]:
    if close is None or prior is None or prior == 0:
        return None, None
    change = close - prior
    pct = (change / prior) * 100.0
    return change, pct


def shape_envelope(raw: dict, *, interval: str) -> dict:
    """Flatten a Schwab /pricehistory response into our display envelope.

    Timestamps are converted from UTC epoch ms to America/New_York and
    rendered per `interval`:
      - minute intervals → "YYYY-MM-DD HH:MM:SS"
      - daily / weekly / monthly → "YYYY-MM-DD"
    """
    raw = raw or {}
    prev_close = _finite(raw.get("previousClose"))
    raw_candles: list[dict] = list(raw.get("candles") or [])

    shaped: list[dict] = []
    prior = prev_close
    for c in raw_candles:
        close = _finite(c.get("close"))
        change, pct = _compute_change(close, prior)
        shaped.append({
            "datetime": _fmt_dt(int(c["datetime"]), interval=interval),
            "open": _finite(c.get("open")),
            "high": _finite(c.get("high")),
            "low": _finite(c.get("low")),
            "close": close,
            "volume": _int(c.get("volume")),
            "change": change,
            "changePct": pct,
        })
        # Carry the *valid* close forward; skip NaNs so the next row's
        # change baseline is still meaningful.
        if close is not None:
            prior = close

    if raw_candles:
        from_iso: str | None = _fmt_iso_ny(int(raw_candles[0]["datetime"]))
        to_iso: str | None = _fmt_iso_ny(int(raw_candles[-1]["datetime"]))
    else:
        from_iso = None
        to_iso = None

    return {
        "symbol": raw.get("symbol", ""),
        "interval": interval,
        "from": from_iso,
        "to": to_iso,
        "previousClose": prev_close,
        "candles": shaped,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_volume(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,d}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{fv:+,.{decimals}f}"
    if fv > 0:
        return f"[green]{s}[/]"
    if fv < 0:
        return f"[red]{s}[/]"
    return s


def _close_cell(close: float | None, open_: float | None) -> str:
    if close is None:
        return "—"
    s = _fmt(close)
    if open_ is None:
        return s
    if close > open_:
        return f"[green]{s}[/]"
    if close < open_:
        return f"[red]{s}[/]"
    return s


def _date_range_label(env: dict) -> str:
    """Extract 'YYYY-MM-DD → YYYY-MM-DD' from the envelope."""
    f = env.get("from") or ""
    t = env.get("to") or ""
    return f"{f[:10]} → {t[:10]}"


def _render_human(env: dict) -> str:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=100,
    )
    symbol = env.get("symbol") or ""
    interval = env.get("interval") or ""
    count = len(env["candles"])
    header = (
        f"[dim]{symbol} — {interval}  "
        f"{_date_range_label(env)}  ({count} candles)[/]"
    )
    console.print(header, highlight=False)
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("Date")
    t.add_column("Open", justify="right")
    t.add_column("High", justify="right")
    t.add_column("Low", justify="right")
    t.add_column("Close", justify="right")
    t.add_column("Change", justify="right")
    t.add_column("Change%", justify="right")
    t.add_column("Volume", justify="right")

    for c in env["candles"]:
        t.add_row(
            c["datetime"],
            _fmt(c["open"]),
            _fmt(c["high"]),
            _fmt(c["low"]),
            _close_cell(c["close"], c["open"]),
            _fmt_signed(c["change"]),
            _fmt_signed(c["changePct"]),
            _fmt_volume(c["volume"]),
        )
    console.print(t)
    return buf.getvalue()


def _render_json(env: dict) -> str:
    return _json.dumps(env, indent=2)


def _render_md(env: dict) -> str:
    symbol = env.get("symbol") or ""
    interval = env.get("interval") or ""
    count = len(env["candles"])
    prev_close = env.get("previousClose")
    prev_str = f"${_fmt(prev_close)}" if prev_close is not None else "—"

    lines = [
        f"# {symbol} — {interval}  {_date_range_label(env)}",
        "",
        f"**Previous close:** {prev_str} · **Candles:** {count}",
        "",
        "| Date | Open | High | Low | Close | Change | Change% | Volume |",
        "|------|------|------|-----|-------|--------|---------|--------|",
    ]
    for c in env["candles"]:
        change = c["change"]
        change_pct = c["changePct"]
        lines.append(
            "| {date} | {o} | {h} | {l} | {cl} | {ch} | {chp} | {v} |".format(
                date=c["datetime"],
                o=_fmt(c["open"]),
                h=_fmt(c["high"]),
                l=_fmt(c["low"]),
                cl=_fmt(c["close"]),
                ch=_md_signed(change),
                chp=_md_signed(change_pct),
                v=_fmt_volume(c["volume"]),
            )
        )
    return "\n".join(lines) + "\n"


def _md_signed(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{fv:+,.{decimals}f}"


def render_history(envelope: dict, *, fmt: Format) -> str:
    if fmt is Format.JSON:
        return _render_json(envelope)
    if fmt is Format.MD:
        return _render_md(envelope)
    if fmt is Format.HUMAN:
        return _render_human(envelope)
    raise NotImplementedError(f"format {fmt} not yet implemented")
