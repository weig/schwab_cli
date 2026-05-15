from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.history import get_history
from schwab_cli.history_spec import (
    IntervalSpecError,
    RangeSpecError,
    parse_interval,
    parse_range,
)
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.history import render_history, shape_envelope
from schwab_cli.session import load as load_session
from datetime import datetime

from schwab_cli.storage import ohlcv_history, vol_history
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


def _try_cache_response(
    symbol: str, *, start, end,
) -> dict | None:
    """Return a Schwab-shaped response dict when the cache fully
    covers ``[start, end]``, otherwise ``None``.

    Used opportunistically on every daily-interval request — the
    caller falls through to the API on ``None`` and then upserts the
    response back into the cache via :func:`_cache_api_response`.
    """
    # The cache buckets by NY trading day. parse_range returns UTC,
    # so convert before lookup or we'd ask for the wrong dates near
    # midnight UTC.
    start_date = start.astimezone(_NY).date()
    end_date   = end.astimezone(_NY).date()
    try:
        with vol_history.connect() as conn:
            if ohlcv_history.gap(
                conn, symbol=symbol, start=start_date, end=end_date,
            ) is not None:
                return None  # cache incomplete — let the API path handle it
            rows = ohlcv_history.read_range(
                conn, symbol=symbol, start=start_date, end=end_date,
            )
    except Exception:
        # If the DB isn't reachable, fall back to the API — better to
        # hit the network than fail the command.
        return None
    return {
        "candles": [
            {"datetime": r["captured_at_ms"],
             "open": r["open"], "high": r["high"],
             "low":  r["low"],  "close": r["close"],
             "volume": r["volume"]}
            for r in rows
        ],
        "symbol": symbol,
    }


def _cache_api_response(symbol: str, response: dict) -> None:
    """Best-effort upsert every daily candle from an API response into
    ``ohlcv_daily``. Called after a fallback API fetch so subsequent
    queries within the same range can be served from the cache.

    Failures here are swallowed — caching is a side effect, the user's
    rendered output is the contract.
    """
    try:
        candles = []
        for c in (response.get("candles") or []):
            dt_ms = c.get("datetime")
            if dt_ms is None:
                continue
            day = (
                datetime.fromtimestamp(int(dt_ms) / 1000, tz=timezone.utc)
                        .astimezone(_NY).date().isoformat()
            )
            candles.append({
                "day": day,
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume") or 0),
                "captured_at_ms": int(dt_ms),
            })
        if not candles:
            return
        with vol_history.connect() as conn:
            ohlcv_history.upsert_candles(
                conn, symbol=symbol, candles=candles,
            )
    except Exception:
        pass  # opportunistic — never break the user's command


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


def run(
    symbol: str,
    *,
    range_str: str,
    interval_str: str,
    as_json: bool,
    as_md: bool,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Resolve to Schwab's canonical symbol so option inputs like
    # "NVDA260501C240" and "NVDA  260501C00240000" both reach the API in
    # the right shape without the caller having to know the OSI padding.
    try:
        ticker = resolve_ticker(symbol)
    except TickerError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    schwab_symbol = ticker.to_schwab_symbol()

    try:
        interval = parse_interval(interval_str)
    except IntervalSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        start, end = parse_range(range_str)
    except RangeSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        # kind discriminator:
        #   "invalid"   → bad grammar              (exit 2)
        #   "ordering"  → start >= end             (exit 1)
        #   "future"    → start is in the future   (exit 1)
        code = 2 if getattr(e, "kind", "invalid") == "invalid" else 1
        raise typer.Exit(code=code)

    # Cache-first read for daily intervals — regardless of whether
    # the symbol is in the ohlcv subscription group. The cache is
    # opportunistically populated by every API fallback below, so
    # frequent `history` queries naturally build up a local store.
    # Non-daily intervals (1min/5min/1wk/1mo) skip the cache entirely
    # — ``ohlcv_daily`` only stores daily candles.
    raw: dict | None = None
    is_daily = interval.frequency_type == "daily"
    if is_daily:
        raw = _try_cache_response(schwab_symbol, start=start, end=end)

    if raw is None:
        client = _client()
        try:
            raw = get_history(
                client,
                schwab_symbol,
                frequency_type=interval.frequency_type,
                frequency=interval.frequency,
                start=start,
                end=end,
            )
        except (ApiError, SessionExpired) as e:
            msg = str(e) if str(e) else type(e).__name__
            typer.secho(msg, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        # Opportunistic backfill: every daily-interval API response
        # seeds the cache. Subsequent queries within this range
        # (including for un-subscribed symbols) skip the network.
        if is_daily:
            _cache_api_response(schwab_symbol, raw)

    envelope = shape_envelope(raw, interval=interval.label)
    if not envelope["candles"]:
        typer.secho(
            f"No candles found for {schwab_symbol} in "
            f"{range_str} at {interval.label}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(render_history(envelope, fmt=fmt))
