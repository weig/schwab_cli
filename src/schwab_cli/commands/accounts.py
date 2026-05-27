from __future__ import annotations

from typing import NoReturn

import typer

from schwab_cli.output.accounts import (
    render_account_result,
    render_accounts_result,
    render_positions_result,
)
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.service.accounts import get_account, get_positions, list_accounts
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)


def _resolve_format(json: bool, md: bool):
    try:
        return pick_format(json, md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


def _handle_not_configured() -> NoReturn:
    typer.secho(
        "No config found. Run `schwab_cli setup` first.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _handle_not_authenticated() -> NoReturn:
    typer.secho(
        "No session found. Run `schwab_cli auth` first.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _handle_api_error(e: Exception) -> NoReturn:
    msg = str(e) if str(e) else type(e).__name__
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def run_list(*, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    try:
        result = list_accounts()
    except NotConfigured:
        _handle_not_configured()
    except NotAuthenticated:
        _handle_not_authenticated()
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_accounts_result(result, fmt))


def run_show(account_number: str, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    try:
        result = get_account(account_number)
    except NotConfigured:
        _handle_not_configured()
    except NotAuthenticated:
        _handle_not_authenticated()
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_account_result(result, fmt))


def run_positions(account_number: str | None, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    try:
        result = get_positions(account_number)
    except NotConfigured:
        _handle_not_configured()
    except NotAuthenticated:
        _handle_not_authenticated()
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_positions_result(result, fmt))
