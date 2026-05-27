from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.history_spec import RangeSpecError, parse_range
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.transactions import render_transactions_result
from schwab_cli.service.transactions import TransactionsService


@cli_errors
def run(
    account: str | None,
    *,
    range_str: str,
    type_filter: str,
    as_json: bool,
    as_md: bool,
    refresh: bool = False,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        start, end = parse_range(range_str)
    except RangeSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        code = 2 if getattr(e, "kind", "invalid") == "invalid" else 1
        raise typer.Exit(code=code)

    result = TransactionsService().get_transactions(
        account,
        start=start,
        end=end,
        type_filter=type_filter,
        refresh=refresh,
    )

    typer.echo(render_transactions_result(result, fmt=fmt))
