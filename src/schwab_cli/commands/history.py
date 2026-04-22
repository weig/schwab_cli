from __future__ import annotations

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

    client = _client()
    try:
        raw = get_history(
            client,
            symbol.upper(),
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
            f"No candles found for {symbol.upper()} in "
            f"{range_str} at {interval.label}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(render_history(envelope, fmt=fmt))
