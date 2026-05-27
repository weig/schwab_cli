from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.fundamentals import render_fundamentals_result
from schwab_cli.service.fundamentals import get_fundamentals


@cli_errors
def run(symbols: list[str], *, as_json: bool, as_md: bool) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    result = get_fundamentals(symbols)

    typer.echo(render_fundamentals_result(result, fmt))
