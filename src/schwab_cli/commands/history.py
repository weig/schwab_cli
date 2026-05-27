from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.history_spec import (
    IntervalSpecError,
    RangeSpecError,
    parse_interval,
    parse_range,
)
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.history import render_history
from schwab_cli.service.history import get_history
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


@cli_errors
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

    # NoCandles (a ServiceError carrying a complete message, exit 1) and the
    # auth / API errors are routed through the @cli_errors decorator.
    result = get_history(
        schwab_symbol,
        frequency_type=interval.frequency_type,
        frequency=interval.frequency,
        label=interval.label,
        start=start,
        end=end,
        range_str=range_str,
    )

    typer.echo(render_history(result.envelope, fmt=fmt))
