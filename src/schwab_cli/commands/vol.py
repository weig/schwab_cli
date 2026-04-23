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

import sqlite3
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
from schwab_cli.storage.vol_history import (
    connect as vol_store_connect,
    read_recent_per_day,
    record_snapshot,
)
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


# Minimum accumulated days before IVP starts rendering a percentile.
# Below this, the IVP cell shows "insufficient history (N/lookback)".
_IVP_MIN_SAMPLE = 30


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
    ivp_lookback: int = 252,
    no_record: bool = False,
    snapshot_only: bool = False,
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

    # IVP: record today's ATM IV, then rank against the accumulated series.
    # A storage failure is surfaced to stderr but never blocks the main
    # render — at worst IVP falls back to "insufficient history" next run.
    ivp_series: list[float] = []
    storage_error: str | None = None
    try:
        with vol_store_connect() as conn:
            if not no_record and atm and atm.get("iv") is not None:
                record_snapshot(
                    conn,
                    symbol=under,
                    spot=spot,
                    atm_iv=atm["iv"],
                    atm_strike=atm["strike"],
                    atm_expiry=atm["expiry"],
                    atm_dte=atm["dte"],
                )
            ivp_series = read_recent_per_day(
                conn, symbol=under, lookback_days=ivp_lookback
            )
    except sqlite3.Error as e:
        storage_error = str(e)

    ivp = _compute_ivp_state(
        series=ivp_series,
        today_iv=atm["iv"] if atm and atm.get("iv") is not None else None,
        lookback=ivp_lookback,
    )

    if snapshot_only:
        # Cron-friendly mode: accumulate silently, don't render.
        if storage_error:
            typer.secho(
                f"vol storage error: {storage_error}",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
        return

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
        "ivp": ivp,
    }

    if storage_error:
        typer.secho(
            f"vol storage warning (IVP may be stale): {storage_error}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    typer.echo(render_vol(envelope, fmt=fmt))


def _compute_ivp_state(
    *,
    series: list[float],
    today_iv: float | None,
    lookback: int,
) -> dict[str, Any]:
    """Map the accumulated IV series + today's IV to the IVP envelope block.

    States (rendered as a dim note next to the value column):

        insufficient   — n < effective_min
        partial        — [effective_min, lookback) days
        ok             — n >= lookback days

    ``effective_min = min(_IVP_MIN_SAMPLE, lookback)`` so a short
    user-chosen lookback (e.g. ``--ivp-lookback=5``) can still resolve
    to a valid percentile once a handful of snapshots exist.
    """
    n = len(series)
    effective_min = min(_IVP_MIN_SAMPLE, lookback)
    if today_iv is None or n < effective_min:
        return {
            "state": "insufficient",
            "value": None,
            "sample_size": n,
            "lookback": lookback,
        }
    pct = percentile_rank(series, today_iv)
    state = "ok" if n >= lookback else "partial"
    return {
        "state": state,
        "value": pct,
        "sample_size": n,
        "lookback": lookback,
    }
