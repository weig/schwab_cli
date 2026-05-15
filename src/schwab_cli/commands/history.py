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
from schwab_cli.storage import ohlcv_history, vol_history
from schwab_cli.storage.groups import GROUP_OHLCV
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


def _is_subscribed_for_ohlcv(symbol: str) -> bool:
    """True when ``symbol`` has an active row in
    ``subscriptions WHERE group_name = 'ohlcv'``."""
    try:
        with vol_history.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM subscriptions "
                "WHERE symbol = ? AND group_name = ? "
                "AND unsubscribed_at IS NULL LIMIT 1",
                (symbol, GROUP_OHLCV),
            ).fetchone()
        return row is not None
    except Exception:
        # If the DB isn't reachable, fall back to the API path —
        # better to hit the network than fail the command.
        return False


def _try_cache_response(
    symbol: str, *, start, end,
) -> dict | None:
    """Return a Schwab-shaped response dict when the cache fully
    covers ``[start, end]``, otherwise ``None`` (caller falls back
    to the API). Used by ``run`` for daily-interval requests on
    OHLCV-subscribed symbols.
    """
    # The cache buckets by NY trading day. parse_range returns UTC,
    # so convert before lookup or we'd ask for the wrong dates near
    # midnight UTC.
    start_date = start.astimezone(_NY).date()
    end_date   = end.astimezone(_NY).date()
    with vol_history.connect() as conn:
        if ohlcv_history.gap(
            conn, symbol=symbol, start=start_date, end=end_date,
        ) is not None:
            return None  # cache incomplete — let the API path handle it
        rows = ohlcv_history.read_range(
            conn, symbol=symbol, start=start_date, end=end_date,
        )
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

    # Cache-first read: for daily intervals on symbols subscribed to
    # the ohlcv group, try the local ohlcv_daily store first. Skips
    # the API entirely when the cache covers the full range; falls
    # through on partial / missing cache or non-daily intervals.
    raw: dict | None = None
    if (interval.frequency_type == "daily"
            and _is_subscribed_for_ohlcv(schwab_symbol)):
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
