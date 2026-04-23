"""`vol` command — IV / HV / HVP / P/C Ratio in two API calls.

Phase 1: IV, HV, HVP, and P/C Ratio are computed from exactly two Schwab
requests (one chain, one price history). IVP is a placeholder —
populated in phase 2 once local accumulation is wired up (see plan at
``docs/superpowers/plans/2026-04-23-schwab-cli-vol-command.md``).

Design constraint: this command must not trigger any side effects in
``option`` or ``greeks``. Both of those stay lean; every data fetch
needed for ``vol`` happens inside this module.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.vol import (
    aggregate_pc,
    percentile_rank,
    pick_atm_contract,
    realized_vol,
    rolling_realized_vol,
)
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.history import get_history
from schwab_cli.history_spec import parse_range
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.vol import render_vol
from schwab_cli.session import load as load_session
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


# ---- client helper ------------------------------------------------------


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


# ---- chain flattener ----------------------------------------------------


def _flatten_chain(raw: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Return (per-expiry blocks, flat contract list) from a /chains response.

    Per-expiry block shape::
        {"expiry": "YYYY-MM-DD", "dte": int, "contracts": [...]}

    Flat contract list mixes calls + puts across every expiry — what
    :func:`aggregate_pc` consumes.
    """
    per_expiry: dict[tuple[str, int], list[dict]] = {}
    flat: list[dict] = []

    for side, map_key in [("C", "callExpDateMap"), ("P", "putExpDateMap")]:
        for expiry_key, strike_map in (raw.get(map_key) or {}).items():
            expiry, _, dte_part = expiry_key.partition(":")
            try:
                dte = int(dte_part)
            except ValueError:
                dte = 0
            bucket = per_expiry.setdefault((expiry, dte), [])
            for _strike, rows in (strike_map or {}).items():
                for row in rows or []:
                    iv_pct = row.get("volatility")
                    iv = (iv_pct / 100.0) if isinstance(iv_pct, (int, float)) else None
                    contract = {
                        "side": side,
                        "strike": row.get("strikePrice"),
                        "iv": iv,
                        "volume": row.get("totalVolume"),
                        "openInterest": row.get("openInterest"),
                        "expiry": expiry,
                        "dte": dte,
                    }
                    bucket.append(contract)
                    flat.append(contract)

    expiries = [
        {"expiry": exp, "dte": dte, "contracts": contracts}
        for (exp, dte), contracts in per_expiry.items()
    ]
    expiries.sort(key=lambda e: e["dte"])
    return expiries, flat


# ---- main entry --------------------------------------------------------


def run(
    symbol: str,
    *,
    hv_window: int = 30,
    hv_lookback: int = 252,
    as_json: bool = False,
    as_md: bool = False,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        ticker = resolve_ticker(symbol)
    except TickerError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if ticker.type != "stock":
        typer.secho(
            f"vol expects a stock ticker, got {ticker.type}: {symbol!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    under = ticker.underlying

    client = _client()

    # Call 1 — chain (wide strikes, ~1y of expirations) for IV + P/C.
    today = date.today()
    try:
        chain_raw = get_chain(
            client,
            under,
            contract_type="ALL",
            strike_count=60,
            from_date=today,
            to_date=today + timedelta(days=365),
        )
    except (ApiError, SessionExpired) as e:
        typer.secho(str(e) or type(e).__name__, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    underlying = (chain_raw or {}).get("underlying") or {}
    spot = underlying.get("last")
    if spot is None:
        typer.secho(f"No spot price in chain response for {under}.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    expiries, flat_contracts = _flatten_chain(chain_raw)

    atm = pick_atm_contract(expiries, spot)
    pc = aggregate_pc(flat_contracts)

    # Call 2 — 1-year daily history for HV + HVP.
    start, end = parse_range(f"-{hv_lookback + hv_window + 20}d..now")
    try:
        history_raw = get_history(
            client,
            under,
            frequency_type="daily",
            frequency=1,
            start=start,
            end=end,
        )
    except (ApiError, SessionExpired) as e:
        typer.secho(str(e) or type(e).__name__, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    closes = [
        c["close"] for c in (history_raw.get("candles") or [])
        if isinstance(c.get("close"), (int, float))
    ]
    hv_today = realized_vol(closes, window=hv_window)
    hv_series = rolling_realized_vol(closes, window=hv_window)
    if hv_series and len(hv_series) > hv_lookback:
        hv_series = hv_series[-hv_lookback:]
    hvp_value = (
        percentile_rank(hv_series, hv_today)
        if hv_today is not None and hv_series
        else None
    )

    envelope = {
        "symbol": under,
        "spot": spot,
        "iv": {
            "value": atm["iv"] if atm else None,
            "expiry": atm["expiry"] if atm else None,
            "dte": atm["dte"] if atm else None,
            "strike": atm["strike"] if atm else None,
        },
        "hv": {"window": hv_window, "value": hv_today},
        "hvp": {
            "lookback": hv_lookback,
            "value": hvp_value,
            "sample_size": len(hv_series),
        },
        "pc": pc,
        "ivp": {
            "state": "not_yet_active",
            "value": None,
            "sample_size": 0,
            "lookback": hv_lookback,
            "message": (
                "phase 2: local accumulation not wired up yet "
                "(see docs/superpowers/plans/2026-04-23-schwab-cli-vol-command.md)"
            ),
        },
    }

    typer.echo(render_vol(envelope, fmt=fmt))
