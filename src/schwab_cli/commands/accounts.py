from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.output.accounts import (
    render_account_result,
    render_accounts_result,
    render_positions_result,
)
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.service.accounts import AccountsService


def _resolve_format(json: bool, md: bool):
    try:
        return pick_format(json, md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


@cli_errors
def run_list(*, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    result = AccountsService().list_accounts()
    typer.echo(render_accounts_result(result, fmt))


@cli_errors
def run_show(account_number: str, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    result = AccountsService().get_account(account_number)
    typer.echo(render_account_result(result, fmt))


@cli_errors
def run_positions(account_number: str | None, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    result = AccountsService().get_positions(account_number)
    typer.echo(render_positions_result(result, fmt))
