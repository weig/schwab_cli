from __future__ import annotations

import math
from io import StringIO
from typing import Any, Literal

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


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _shape_contract(raw: dict, side: Literal["C", "P"]) -> dict:
    iv_pct = _finite(raw.get("volatility"))
    return {
        "optionSymbol": (raw.get("symbol") or ""),
        "side": side,
        "strike": _finite(raw.get("strikePrice")),
        "bid": _finite(raw.get("bid")),
        "ask": _finite(raw.get("ask")),
        "last": _finite(raw.get("last")),
        "delta": _finite(raw.get("delta")),
        "iv": (iv_pct / 100.0) if iv_pct is not None else None,
        "gamma": _finite(raw.get("gamma")),
        "theta": _finite(raw.get("theta")),
        "vega": _finite(raw.get("vega")),
        "volume": _int(raw.get("totalVolume")),
        "openInterest": _int(raw.get("openInterest")),
        "mark": _finite(raw.get("mark")),
        "bidSize": _int(raw.get("bidSize")),
        "askSize": _int(raw.get("askSize")),
        "lastSize": _int(raw.get("lastSize")),
        "open": _finite(raw.get("openPrice")),
        "high": _finite(raw.get("highPrice")),
        "low": _finite(raw.get("lowPrice")),
        "close": _finite(raw.get("closePrice")),
        "rho": _finite(raw.get("rho")),
        "timeValue": _finite(raw.get("timeValue")),
        "intrinsic": _finite(raw.get("intrinsicValue")),
        "inTheMoney": bool(raw.get("inTheMoney")),
        "multiplier": _int(raw.get("multiplier")),
        "settlementType": raw.get("settlementType"),
    }


def shape_envelope(raw: dict, *, strike_count: int | None = None) -> dict:
    """Flatten a Schwab /chains response into our display envelope.

    If `strike_count` is given, keeps only the N strikes whose prices are
    closest to the underlying spot — both the call and the put at each kept
    strike survive the trim. If the underlying spot is unavailable (e.g.
    Schwab response omits `underlying.last` or it's non-numeric), trimming
    is silently skipped and all contracts are returned.
    """
    underlying_raw = (raw or {}).get("underlying") or {}
    underlying = {
        "last": _finite(underlying_raw.get("last")),
        "netChange": _finite(underlying_raw.get("change")),
        "pctChange": _finite(underlying_raw.get("percentChange")),
    }

    contracts: list[dict] = []
    expiry: str | None = None
    dte: int | None = None

    for source_key, side in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        date_map = (raw or {}).get(source_key) or {}
        for exp_key, strike_map in date_map.items():
            for _strike_str, contract_list in (strike_map or {}).items():
                for c in (contract_list or []):
                    if expiry is None:
                        expiry = c.get("expirationDate") or exp_key.split(":")[0]
                        dte = c.get("daysToExpiration")
                    contracts.append(_shape_contract(c, side))

    if strike_count is not None and contracts:
        spot = underlying["last"]
        if spot is not None:
            unique_strikes = {c["strike"] for c in contracts if c["strike"] is not None}
            strikes = sorted(unique_strikes, key=lambda s: (abs(s - spot), s))
            keep = set(strikes[:strike_count])
            contracts = [c for c in contracts if c["strike"] in keep]

    contracts.sort(key=lambda r: (r["strike"] if r["strike"] is not None else 0.0,
                                  0 if r["side"] == "C" else 1))

    return {
        "symbol": (raw or {}).get("symbol", ""),
        "expiry": expiry,
        "dte": dte,
        "underlying": underlying,
        "contracts": contracts,
    }


_HEADER_FMT = "{symbol} — {expiry} ({dte} DTE)    Spot: ${spot}  ({change} / {pct}%)"


def _fmt(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed(v, decimals: int = 2) -> str:
    s = _fmt(v, decimals)
    if s == "—":
        return s
    fv = float(v)
    if fv > 0:
        return f"[green]{s}[/]"
    if fv < 0:
        return f"[red]{s}[/]"
    return s


def _header_line(env: dict) -> str:
    u = env.get("underlying") or {}
    return _HEADER_FMT.format(
        symbol=env.get("symbol") or "",
        expiry=env.get("expiry") or "",
        dte=env.get("dte") if env.get("dte") is not None else "?",
        spot=_fmt(u.get("last")),
        change=_fmt(u.get("netChange")),
        pct=_fmt(u.get("pctChange")),
    )


def _atm_strike(env: dict) -> float | None:
    u = env.get("underlying") or {}
    spot = u.get("last")
    if spot is None:
        return None
    strikes = sorted({c["strike"] for c in env["contracts"] if c["strike"] is not None})
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def _pairs_by_strike(env: dict) -> list[tuple[float, dict | None, dict | None]]:
    """Zip calls and puts by strike (ascending). None when side missing."""
    by_strike: dict[float, dict[str, dict]] = {}
    for c in env["contracts"]:
        if c["strike"] is None:
            continue
        by_strike.setdefault(c["strike"], {})[c["side"]] = c
    return [
        (strike, by_strike[strike].get("C"), by_strike[strike].get("P"))
        for strike in sorted(by_strike)
    ]


def _console(width: int | None) -> tuple[Console, StringIO]:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=width or 120,
    )
    return console, buf


def _bold_if(itm: bool, s: str) -> str:
    """Wrap `s` in bold markup when ITM. Leaves already-colored spans alone
    so that ANSI color codes emit without the bold SGR prefix."""
    if not itm or s == "—":
        return s
    # If the cell already carries [green]/[red] markup we don't bold it —
    # colored cells remain plain-colored even on ITM rows, while neutral
    # cells become bold. This keeps plain `\x1b[32m` / `\x1b[31m` visible
    # alongside plain `\x1b[1m` bold codes for neutral cells.
    if s.startswith("[green]") or s.startswith("[red]"):
        return s
    return f"[bold]{s}[/]"


def _render_human_a(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    atm = _atm_strike(env)
    # highlight=False — rich's default number/date highlighter would otherwise
    # wrap "2027-01-15" and "142.35" in cyan SGR codes and break literal
    # substring checks by consumers.
    console.print(_header_line(env), highlight=False)
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    # Call side (outer → inner): Δ, Last, Ask, Bid, Vol, OI
    t.add_column("Δ", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("OI", justify="right")
    t.add_column("STRIKE", justify="center")
    # Put side (inner → outer): OI, Vol, Bid, Ask, Last, Δ
    t.add_column("OI", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Δ", justify="right")

    for strike, call, put in _pairs_by_strike(env):
        strike_label = f"{strike:,.2f}"
        if atm is not None and strike == atm:
            strike_label = f"{strike_label} ←"
        itm = bool((call and call["inTheMoney"]) or (put and put["inTheMoney"]))
        t.add_row(
            _bold_if(itm, _fmt_signed((call or {}).get("delta"))),
            _bold_if(itm, _fmt_signed((call or {}).get("last"))),
            _bold_if(itm, _fmt((call or {}).get("ask"))),
            _bold_if(itm, _fmt((call or {}).get("bid"))),
            _bold_if(itm, _fmt((call or {}).get("volume"), 0)),
            _bold_if(itm, _fmt((call or {}).get("openInterest"), 0)),
            _bold_if(itm, strike_label),
            _bold_if(itm, _fmt((put or {}).get("openInterest"), 0)),
            _bold_if(itm, _fmt((put or {}).get("volume"), 0)),
            _bold_if(itm, _fmt((put or {}).get("bid"))),
            _bold_if(itm, _fmt((put or {}).get("ask"))),
            _bold_if(itm, _fmt_signed((put or {}).get("last"))),
            _bold_if(itm, _fmt_signed((put or {}).get("delta"))),
        )

    console.print(t)
    return buf.getvalue()


def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.HUMAN:
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
