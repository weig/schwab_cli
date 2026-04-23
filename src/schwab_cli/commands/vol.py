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
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.bs import implied_vol
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
    SOURCE_OBSERVED,
    SOURCE_SYNTHETIC,
    connect as vol_store_connect,
    count_by_source,
    read_recent_per_day_with_source,
    record_snapshot,
)
from schwab_cli.ticker import Ticker, TickerError, resolve as resolve_ticker


# Minimum accumulated days before IVP starts rendering a percentile.
# Below this, the IVP cell shows "insufficient history (N/lookback)".
_IVP_MIN_SAMPLE = 30

# Fallback risk-free rate used by the BS backfill (3-month T-bill
# approximation). Error contribution vs the "true" daily rate is small;
# sensitivity of short-dated IV to r is on the order of 0.1%/pct.
_BACKFILL_RISK_FREE_RATE = 0.045

_NY = ZoneInfo("America/New_York")


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
        # Expiry window goes out ~1.5 years so the backfill can find a
        # long-dated LEAPS with real trading history without a second
        # /chains call.
        chain_raw = get_chain(
            client,
            under,
            contract_type="ALL",
            strike_count=60,
            from_date=today,
            to_date=today + timedelta(days=540),
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

    # IVP is computed against a long-dated reference contract whose price
    # history can be backfilled across a full year. Mixing that series
    # with today's near-term IV gives nonsense percentiles (near-term is
    # structurally lower than LEAPS). We pick the LEAPS now so we can
    # surface its current IV next to the near-term one and record *that*
    # value as today's snapshot — keeping display, storage, and
    # percentile all term-consistent.
    ivp_ref = _pick_backfill_contract(expiries, atm["strike"]) if atm else None

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
    #
    # First-run-per-symbol also attempts a BS backfill so IVP can be
    # meaningful immediately. The backfill costs one extra history call
    # on the current ATM contract; underlying history is already in hand
    # from the HV calculation.
    ivp_series_tagged: list[tuple[float, str]] = []
    storage_error: str | None = None
    # What gets recorded as "today's observation" must match the reference
    # contract the backfill uses; otherwise the IVP series mixes tenors.
    snapshot_contract = ivp_ref if (ivp_ref and ivp_ref.get("iv") is not None) else atm
    try:
        with vol_store_connect() as conn:
            if (
                not no_record
                and snapshot_contract
                and snapshot_contract.get("iv") is not None
            ):
                record_snapshot(
                    conn,
                    symbol=under,
                    spot=spot,
                    atm_iv=snapshot_contract["iv"],
                    atm_strike=snapshot_contract["strike"],
                    atm_expiry=snapshot_contract["expiry"],
                    atm_dte=snapshot_contract["dte"],
                    source=SOURCE_OBSERVED,
                )
            # Auto-backfill: populate synthetic history once per symbol.
            # Trigger when no synthetics exist yet AND the user hasn't
            # accumulated enough real observations to support a
            # percentile on their own. This works both for brand-new
            # symbols and for users who have a handful of legacy
            # observed rows from pre-backfill runs.
            counts = count_by_source(conn, symbol=under)
            if (
                not no_record
                and atm
                and atm.get("iv") is not None
                and counts[SOURCE_SYNTHETIC] == 0
                and counts[SOURCE_OBSERVED] < _IVP_MIN_SAMPLE
            ):
                n_synth = _backfill_synthetic_iv(
                    conn,
                    client=client,
                    symbol=under,
                    atm=atm,
                    expiries=expiries,
                    underlying_closes=closes,
                    underlying_candles=history_raw.get("candles") or [],
                )
                # Only mention the backfill to human readers — in JSON/MD
                # modes or --snapshot-only the extra stderr line would
                # fight with piping into `jq` / `sed` etc.
                if n_synth and not (as_json or as_md or snapshot_only):
                    typer.secho(
                        f"vol: backfilled {n_synth} synthetic IV days "
                        f"for {under} from option + underlying history.",
                        fg=typer.colors.CYAN,
                        err=True,
                    )
            ivp_series_tagged = read_recent_per_day_with_source(
                conn, symbol=under, lookback_days=ivp_lookback
            )
    except sqlite3.Error as e:
        storage_error = str(e)

    ivp = _compute_ivp_state(
        series_tagged=ivp_series_tagged,
        today_iv=(
            snapshot_contract["iv"]
            if snapshot_contract and snapshot_contract.get("iv") is not None
            else None
        ),
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
        # Long-dated IV the IVP percentile is actually computed against.
        # Surfaced here so users can see why IVP sits where it does.
        "iv_ref": {
            "value": ivp_ref["iv"] if ivp_ref and ivp_ref.get("iv") is not None else None,
            "expiry": ivp_ref["expiry"] if ivp_ref else None,
            "dte": ivp_ref["dte"] if ivp_ref else None,
            "strike": ivp_ref["strike"] if ivp_ref else None,
        } if ivp_ref else None,
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
    series_tagged: list[tuple[float, str]],
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

    The emitted block also carries ``observed`` / ``synthetic`` counts
    so the renderer can annotate IVP with a "N synthetic, N observed"
    breakdown when the auto-backfill has contributed to the series.
    """
    n = len(series_tagged)
    observed = sum(1 for _, src in series_tagged if src == SOURCE_OBSERVED)
    synthetic = n - observed
    effective_min = min(_IVP_MIN_SAMPLE, lookback)
    common: dict[str, Any] = {
        "sample_size": n,
        "observed": observed,
        "synthetic": synthetic,
        "lookback": lookback,
    }
    if today_iv is None or n < effective_min:
        return {"state": "insufficient", "value": None, **common}
    series_values = [iv for iv, _ in series_tagged]
    pct = percentile_rank(series_values, today_iv)
    state = "ok" if n >= lookback else "partial"
    return {"state": state, "value": pct, **common}


# ---- backfill ----------------------------------------------------------


def _backfill_synthetic_iv(
    conn,
    *,
    client: SchwabClient,
    symbol: str,
    atm: dict[str, Any],
    expiries: list[dict[str, Any]],
    underlying_closes: list[float],
    underlying_candles: list[dict[str, Any]],
) -> int:
    """Populate the store with a 1-year synthetic ATM-IV series.

    Fetches ~1y of daily candles for a LONG-DATED reference contract
    (LEAPS near today's spot), joins by datetime against the underlying
    candles already in hand (for HV), BS-solves IV per day, and inserts
    rows with source='synthetic'. Using a long-dated strike — not
    today's near-term ATM — is load-bearing: near-term contracts only
    have a few weeks of trading history, far too short to populate a
    252-day IVP.

    Returns the number of synthetic rows written. Never raises on API
    or math failures; the command continues with whatever got recorded.
    """
    # A LEAPS-strike-near-spot has been listed for months-to-years, so
    # its price history covers enough calendar time for the backfill.
    # Fall back to today's ATM if no LEAPS is available.
    backfill = _pick_backfill_contract(expiries, atm["strike"]) or atm
    try:
        option_sym = _build_atm_call_symbol(symbol, backfill)
    except ValueError:
        return 0

    # Use the same range we used for the underlying history.
    try:
        start, end = parse_range("-280d..now")
        opt_raw = get_history(
            client, option_sym,
            frequency_type="daily", frequency=1,
            start=start, end=end,
        )
    except Exception:
        return 0

    opt_candles = opt_raw.get("candles") or []
    if not opt_candles:
        return 0

    # Align by NY trading day (underlying and option candles may use
    # slightly different intraday timestamps but share the same session).
    und_by_day: dict[str, tuple[int, float]] = {}
    for c in underlying_candles:
        dt_ms = c.get("datetime")
        close = c.get("close")
        if not isinstance(dt_ms, (int, float)) or close is None:
            continue
        day = _ny_date_of_ms(int(dt_ms))
        und_by_day[day] = (int(dt_ms), float(close))

    expiry_date = _parse_iso_date(backfill["expiry"])
    if expiry_date is None:
        return 0
    strike = float(backfill["strike"])

    written = 0
    for oc in opt_candles:
        dt_ms = oc.get("datetime")
        opt_close = oc.get("close")
        if not isinstance(dt_ms, (int, float)) or opt_close is None:
            continue
        day = _ny_date_of_ms(int(dt_ms))
        und_entry = und_by_day.get(day)
        if und_entry is None:
            continue
        _und_ms, und_close = und_entry
        # DTE at the historical moment.
        obs_date = _parse_iso_date(day)
        if obs_date is None:
            continue
        T = (expiry_date - obs_date).days / 365.0
        if T <= 0:
            continue
        iv = implied_vol(
            float(opt_close), und_close, strike, T,
            _BACKFILL_RISK_FREE_RATE, is_call=True,
        )
        # Sanity-filter: reject absurd solver outputs from illiquid days.
        if iv is None or iv <= 0.02 or iv > 3.0:
            continue
        record_snapshot(
            conn,
            symbol=symbol,
            spot=und_close,
            atm_iv=iv,
            atm_strike=strike,
            atm_expiry=backfill["expiry"],
            atm_dte=(expiry_date - obs_date).days,
            captured_at_ms=int(dt_ms),
            source=SOURCE_SYNTHETIC,
        )
        written += 1
    return written


def _pick_backfill_contract(
    expiries: list[dict[str, Any]],
    target_strike: float,
) -> dict[str, Any] | None:
    """Pick a long-dated reference contract for the IV backfill.

    Preference: DTE >= 180 (months of trading history available), strike
    closest to the current ATM strike. Returns the picked contract's
    current IV (midpoint of call + put) so it can be used as "today's
    IV" in the IVP calculation — keeping the percentile term-consistent
    with the historical synthetic series we back-compute from this same
    contract's price history.

    If no expiry qualifies, returns None so the caller falls back to
    today's near-term ATM for both display and IVP (biased but better
    than nothing on illiquid names).
    """
    long_dated = [e for e in expiries if e.get("dte", 0) >= 180]
    if not long_dated:
        return None
    best_exp = min(long_dated, key=lambda e: abs(e["dte"] - 365))
    contracts = best_exp.get("contracts", [])
    by_strike: dict[float, list[dict[str, Any]]] = {}
    for c in contracts:
        if c.get("strike") is None:
            continue
        by_strike.setdefault(c["strike"], []).append(c)
    if not by_strike:
        return None
    strike = min(by_strike.keys(), key=lambda s: abs(s - target_strike))
    ivs = [c["iv"] for c in by_strike[strike] if c.get("iv") is not None]
    iv = (sum(ivs) / len(ivs)) if ivs else None
    return {
        "expiry": best_exp["expiry"],
        "dte": best_exp["dte"],
        "strike": strike,
        "iv": iv,
    }


def _build_atm_call_symbol(symbol: str, atm: dict[str, Any]) -> str:
    """Return the Schwab-canonical OSI symbol for the ATM call of the
    contract picked by :func:`pick_atm_contract`."""
    from schwab_cli.ticker import OptionPart, Ticker

    date_str = atm["expiry"].replace("-", "")  # YYYY-MM-DD → YYYYMMDD
    return Ticker(
        type="option",
        underlying=symbol,
        option=OptionPart(date=date_str, type="C", strike=float(atm["strike"])),
    ).to_schwab_symbol()


def _ny_date_of_ms(dt_ms: int) -> str:
    """Return ISO date (YYYY-MM-DD) of the NY calendar day for an epoch-ms."""
    return (
        datetime.fromtimestamp(dt_ms / 1000, tz=timezone.utc)
        .astimezone(_NY).date().isoformat()
    )


def _parse_iso_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
