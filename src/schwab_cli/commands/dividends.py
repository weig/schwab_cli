from __future__ import annotations

import typer

from schwab_cli.output.dividends import render_dividends_result
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)
from schwab_cli.service.dividends import get_dividends


def run(
    symbols: list[str],
    *,
    upcoming: bool,
    within_days: int,
    as_json: bool,
    as_md: bool,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        result = get_dividends(symbols)
    except NotConfigured:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except NotAuthenticated:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    upcoming_window = within_days if upcoming else None
    typer.echo(
        render_dividends_result(
            result, fmt,
            upcoming_within_days=upcoming_window,
        )
    )
