from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.output.dividends import render_dividends_result
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.service.dividends import DividendsService


@cli_errors
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

    result = DividendsService().get_dividends(symbols)

    upcoming_window = within_days if upcoming else None
    typer.echo(
        render_dividends_result(
            result, fmt,
            upcoming_within_days=upcoming_window,
        )
    )
