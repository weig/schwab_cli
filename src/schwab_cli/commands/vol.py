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
    interp_iv_in_variance,
    percentile_rank,
    pick_atm_contract,
    pick_atm_curve,
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


# Minimum accumulated days required before the percentile itself is
# rendered. 90 ≈ one quarter — below that, a "percentile" reading is
# dominated by the specific regime captured in the short window and
# conveys false precision. The IVP cell falls back to showing the
# sample's IV range + today's value so the user still has a useful
# data point, without pretending it's a real 52-week IVP.
_IVP_MIN_SAMPLE = 90

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
#
# Single-source-of-truth lives in api/chains.py — every consumer of the
# Schwab /chains endpoint reaches through that helper so we can't drift
# between the legacy `vol` and new `dataset` paths.
from schwab_cli.api.chains import flatten_chain as _flatten_chain  # noqa: E402


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
    ivr_ivp: dict = {
        "ivr": None, "ivp": None, "n_days": 0,
        "source": "insufficient", "backfilled": False, "low_history": True,
    }
    # What gets recorded as "today's observation" must match the reference
    # contract the backfill uses; otherwise the IVP series mixes tenors.
    snapshot_contract = ivp_ref if (ivp_ref and ivp_ref.get("iv") is not None) else atm
    # Compute the 30-day constant-maturity ATM IV from today's chain for
    # the tier-1 IVR/IVP path.
    atm_curve = pick_atm_curve(expiries, spot)
    interp_30d = interp_iv_in_variance(atm_curve, 30)
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
                # Per-day progress lines stream to stdout in human mode
                # only — keeps JSON / MD / snapshot-only output clean for
                # piping.
                _bf_progress = (
                    (lambda line: typer.secho(line, fg=typer.colors.CYAN))
                    if not (as_json or as_md or snapshot_only) else None
                )
                n_synth = _backfill_synthetic_iv(
                    conn,
                    client=client,
                    symbol=under,
                    atm=atm,
                    expiries=expiries,
                    underlying_closes=closes,
                    underlying_candles=history_raw.get("candles") or [],
                    progress=_bf_progress,
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
            # 3-tier IVR/IVP: prefer atm_iv_30d series; fall back to
            # legacy atm_iv; tier-3 delegates backfill to the same
            # function used above (no-op if already ran this invocation).
            ivr_ivp = compute_iv_rank_and_percentile(
                conn,
                symbol=under,
                today_iv_30d=interp_30d,
                today_atm_iv=atm["iv"] if atm and atm.get("iv") is not None else None,
                lookback=ivp_lookback,
                backfill_callable=None,  # backfill already handled above
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
        "ivr_ivp": ivr_ivp,
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

    States (rendered next to the value column):

        insufficient   — n < effective_min. Value is ``None`` — we
                         deliberately don't show a percentile that
                         reads like a real IVP at this sample size.
                         ``range_min`` / ``range_max`` / ``today_iv``
                         are populated so the renderer can surface
                         "today 38.5% vs window 41.0–47.7%".
        partial        — [effective_min, lookback) days — value shown,
                         annotated with the actual sample size.
        ok             — n >= lookback days.

    ``effective_min = min(_IVP_MIN_SAMPLE, lookback)`` — a user-chosen
    short lookback (``--ivp-lookback=30``) can still resolve to a value
    once the sample matches the lookback, without forcing the 90-day
    minimum everywhere.

    Also carries ``observed`` / ``synthetic`` counts so the renderer
    can annotate the source breakdown.
    """
    n = len(series_tagged)
    observed = sum(1 for _, src in series_tagged if src == SOURCE_OBSERVED)
    synthetic = n - observed
    effective_min = min(_IVP_MIN_SAMPLE, lookback)
    series_values = [iv for iv, _ in series_tagged]
    common: dict[str, Any] = {
        "sample_size": n,
        "observed": observed,
        "synthetic": synthetic,
        "lookback": lookback,
        "today_iv": today_iv,
        "range_min": min(series_values) if series_values else None,
        "range_max": max(series_values) if series_values else None,
    }
    if today_iv is None or n < effective_min:
        return {"state": "insufficient", "value": None, **common}
    pct = percentile_rank(series_values, today_iv)
    state = "ok" if n >= lookback else "partial"
    return {"state": state, "value": pct, **common}


# ---- backfill ----------------------------------------------------------


def _existing_ny_days(conn, symbol: str) -> dict[str, str]:
    """Map ``YYYY-MM-DD`` (NY calendar day) → ``'observed' | 'synthetic'``.

    Used by the backfill loop to decide whether a candidate day should
    be skipped (already have data) or written. Live observations
    win over backfill rows when a single NY day has both.
    """
    rows = conn.execute(
        "SELECT captured_at_ms, source FROM vol_snapshots WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        day = _ny_date_of_ms(int(r["captured_at_ms"]))
        if out.get(day) != SOURCE_OBSERVED:
            out[day] = r["source"] or SOURCE_OBSERVED
    return out


def _emit_backfill(
    progress, *, symbol: str, day: str, status: str,
) -> None:
    """Render one Backfill progress line. ``progress`` is the callable
    or ``None`` (silent in JSON / MD / snapshot-only output modes)."""
    if progress is None:
        return
    if status == "wrote":
        progress(f"Backfill {symbol} volatility {day}")
    elif status == "skipped_live":
        progress(f"Backfill {symbol} volatility {day} (Skipped, live data existed)")
    elif status == "skipped_backfill":
        progress(f"Backfill {symbol} volatility {day} (Skipped, backfill data existed)")


def _backfill_synthetic_iv(
    conn,
    *,
    client: SchwabClient,
    symbol: str,
    atm: dict[str, Any],
    expiries: list[dict[str, Any]],
    underlying_closes: list[float],
    underlying_candles: list[dict[str, Any]],
    progress=None,
) -> int:
    """Populate the store with a 1-year synthetic ATM-IV series.

    Strategy: **stitched multi-strike**. We pick a long-dated expiry
    (LEAPS ~365 DTE), then fetch price history for several distinct
    strikes that cover the underlying's 1-year spot range. For each
    historical trading day we pick the strike closest to *that day's*
    spot and BS-solve IV from its close. This gives us a series that
    stays moneyness-consistent even as spot drifted over the year,
    which a single-strike series can't — strikes get listed by OCC
    progressively as spot moves, so "today's ATM strike" typically
    hasn't been trading for a full year.

    Falls back to a single-strike backfill (today's LEAPS at current
    spot) when no long-dated expiry is available — illiquid names, or
    when the chain window is too narrow.

    Returns the number of synthetic rows written. Never raises on API
    or math failures; the command continues with whatever got recorded.
    """
    long_exp = _pick_long_dated_expiry(expiries)
    if long_exp is not None:
        written = _stitched_backfill(
            conn, client,
            symbol=symbol,
            expiry=long_exp,
            underlying_candles=underlying_candles,
            progress=progress,
        )
        if written > 0:
            return written
    # Fallback: use today's near-term ATM.
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

    existing_days = _existing_ny_days(conn, symbol)
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
        if existing_days.get(day) == SOURCE_OBSERVED:
            _emit_backfill(progress, symbol=symbol, day=day,
                           status="skipped_live")
            continue
        if existing_days.get(day) == SOURCE_SYNTHETIC:
            _emit_backfill(progress, symbol=symbol, day=day,
                           status="skipped_backfill")
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
        existing_days[day] = SOURCE_SYNTHETIC
        _emit_backfill(progress, symbol=symbol, day=day, status="wrote")
        written += 1
    return written


# ---- stitched backfill -------------------------------------------------


# Number of distinct strikes we fetch history for when building a
# stitched series. Five is enough to cover a year of NVDA-like drift
# without blowing past Schwab's rate limits on a single first-run.
_STITCH_STRIKE_COUNT = 5

# Rate for BS solve. 3-month T-bill stand-in; error contribution to IV
# is ~10 bps/1% rate sensitivity for short-dated ATM options.
# (Already declared above but repeated here to make the stitched path
# self-contained conceptually.)


def _pick_long_dated_expiry(
    expiries: list[dict[str, Any]],
    *,
    min_dte: int = 180,
) -> dict[str, Any] | None:
    """Return the expiry whose DTE is closest to 365, restricted to
    ``min_dte`` or later. Used as the fixed LEAPS reference for the
    stitched backfill — we reuse this contract for every strike so the
    synthesised IV series stays on a single term.
    """
    candidates = [e for e in expiries if e.get("dte", 0) >= min_dte]
    if not candidates:
        return None
    return min(candidates, key=lambda e: abs(e["dte"] - 365))


def _choose_stitch_strikes(
    spot_values: list[float],
    available_strikes: list[float],
    *,
    count: int = _STITCH_STRIKE_COUNT,
) -> list[float]:
    """Pick up to ``count`` strikes that cover the spot distribution.

    Targets the 10/30/50/70/90th percentiles of the year's spot prices
    (so high-density regimes get more coverage), rounds each to the
    nearest available strike, and de-duplicates.
    """
    if not spot_values or not available_strikes:
        return []
    s_sorted = sorted(spot_values)
    pct_anchors = [10, 30, 50, 70, 90][:count]
    n = len(s_sorted)
    targets = [s_sorted[min(n - 1, max(0, int(p * n / 100)))] for p in pct_anchors]
    chosen: set[float] = set()
    for t in targets:
        k = min(available_strikes, key=lambda s: abs(s - t))
        chosen.add(k)
    return sorted(chosen)


def _stitched_backfill(
    conn,
    client: SchwabClient,
    *,
    symbol: str,
    expiry: dict[str, Any],
    underlying_candles: list[dict[str, Any]],
    progress=None,
) -> int:
    """Fetch multiple strikes on ``expiry`` and emit one IV row per
    underlying trading day, using the strike closest to that day's spot.
    """
    und_by_day: dict[str, tuple[int, float]] = {}
    for c in underlying_candles:
        dt_ms = c.get("datetime")
        close = c.get("close")
        if not isinstance(dt_ms, (int, float)) or close is None:
            continue
        und_by_day[_ny_date_of_ms(int(dt_ms))] = (int(dt_ms), float(close))
    if len(und_by_day) < 30:
        return 0

    available_strikes = sorted({
        c["strike"] for c in expiry.get("contracts", [])
        if c.get("strike") is not None
    })
    spot_values = [s for _ms, s in und_by_day.values()]
    strikes = _choose_stitch_strikes(spot_values, available_strikes)
    if not strikes:
        return 0

    # Fetch a year of daily candles for each chosen strike. Soft-fail on
    # per-strike errors — we emit what we can from what succeeds.
    try:
        start, end = parse_range("-400d..now")
    except Exception:
        return 0

    from schwab_cli.ticker import OptionPart, Ticker

    date_str = expiry["expiry"].replace("-", "")
    opt_per_day: dict[str, list[tuple[float, float, int]]] = {}
    fetched = 0
    for strike in strikes:
        sym = Ticker(
            type="option",
            underlying=symbol,
            option=OptionPart(date=date_str, type="C", strike=float(strike)),
        ).to_schwab_symbol()
        try:
            raw = get_history(
                client, sym,
                frequency_type="daily", frequency=1,
                start=start, end=end,
            )
        except Exception:
            continue
        candles = raw.get("candles") or []
        if not candles:
            continue
        fetched += 1
        for c in candles:
            dt_ms = c.get("datetime")
            close = c.get("close")
            if not isinstance(dt_ms, (int, float)) or close is None:
                continue
            day = _ny_date_of_ms(int(dt_ms))
            opt_per_day.setdefault(day, []).append(
                (float(strike), float(close), int(dt_ms))
            )
    if fetched == 0:
        return 0

    expiry_date = _parse_iso_date(expiry["expiry"])
    if expiry_date is None:
        return 0

    existing_days = _existing_ny_days(conn, symbol)
    written = 0
    for day, (_und_ms, spot) in sorted(und_by_day.items()):
        day_obj = _parse_iso_date(day)
        if day_obj is None:
            continue
        T = (expiry_date - day_obj).days / 365.0
        if T <= 0:
            continue
        if existing_days.get(day) == SOURCE_OBSERVED:
            _emit_backfill(progress, symbol=symbol, day=day,
                           status="skipped_live")
            continue
        if existing_days.get(day) == SOURCE_SYNTHETIC:
            _emit_backfill(progress, symbol=symbol, day=day,
                           status="skipped_backfill")
            continue
        candidates = opt_per_day.get(day)
        if not candidates:
            continue
        strike, opt_close, dt_ms = min(candidates, key=lambda t: abs(t[0] - spot))
        iv = implied_vol(
            opt_close, spot, strike, T,
            _BACKFILL_RISK_FREE_RATE, is_call=True,
        )
        if iv is None or iv <= 0.02 or iv > 3.0:
            continue
        record_snapshot(
            conn,
            symbol=symbol,
            spot=spot,
            atm_iv=iv,
            atm_strike=strike,
            atm_expiry=expiry["expiry"],
            atm_dte=(expiry_date - day_obj).days,
            captured_at_ms=dt_ms,
            source=SOURCE_SYNTHETIC,
        )
        existing_days[day] = SOURCE_SYNTHETIC
        _emit_backfill(progress, symbol=symbol, day=day, status="wrote")
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


# ---- 3-tier IVR/IVP resolver -------------------------------------------


def compute_iv_rank_and_percentile(
    conn,
    *,
    symbol: str,
    today_iv_30d: float | None,
    today_atm_iv: float | None,
    lookback: int = 252,
    backfill_callable=None,
) -> dict:
    """Tier 1 → 2 → 3 IVR/IVP resolver.

    ``backfill_callable`` is the function used to populate synthetic
    rows on tier-3 fallback; in production it's
    :func:`_backfill_synthetic_iv`. Tests pass ``None`` to skip the
    network-touching step.
    """
    from schwab_cli.storage.vol_history import (
        read_atm_iv_30d_per_day, read_recent_per_day_with_source,
    )

    # TIER 1.
    if today_iv_30d is not None:
        series = read_atm_iv_30d_per_day(
            conn, symbol=symbol, lookback_days=lookback
        )
        if len(series) >= 120:
            return {
                "ivr":        _ivr_from(series, today_iv_30d),
                "ivp":        _ivp_from(series, today_iv_30d),
                "n_days":     len(series),
                "source":     "atm_iv_30d",
                "backfilled": False,
            }

    # TIER 2 / 3.
    legacy = read_recent_per_day_with_source(
        conn, symbol=symbol, lookback_days=lookback
    )
    backfilled = any(s == "synthetic" for _, s in legacy)
    legacy_ivs = [iv for iv, _ in legacy]

    if today_atm_iv is not None and len(legacy_ivs) >= 120:
        return {
            "ivr":        _ivr_from(legacy_ivs, today_atm_iv),
            "ivp":        _ivp_from(legacy_ivs, today_atm_iv),
            "n_days":     len(legacy_ivs),
            "source":     "atm_iv (legacy + synthetic)" if backfilled
                          else "atm_iv (legacy)",
            "backfilled": backfilled,
        }

    if backfill_callable is not None:
        backfill_callable()
        legacy = read_recent_per_day_with_source(
            conn, symbol=symbol, lookback_days=lookback
        )
        legacy_ivs = [iv for iv, _ in legacy]
        if today_atm_iv is not None and len(legacy_ivs) >= 120:
            return {
                "ivr":        _ivr_from(legacy_ivs, today_atm_iv),
                "ivp":        _ivp_from(legacy_ivs, today_atm_iv),
                "n_days":     len(legacy_ivs),
                "source":     "atm_iv (legacy + synthetic)",
                "backfilled": True,
            }

    return {
        "ivr":        None,
        "ivp":        None,
        "n_days":     len(legacy_ivs),
        "source":     "insufficient",
        "backfilled": backfilled,
        "low_history": True,
    }


def _ivr_from(series: list[float], today: float) -> float:
    lo, hi = min(series), max(series)
    if hi <= lo:
        return 50.0
    return 100.0 * (today - lo) / (hi - lo)


def _ivp_from(series: list[float], today: float) -> float:
    from schwab_cli.analytics.vol import percentile_rank
    return percentile_rank(series, today)
