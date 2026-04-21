from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.accounts import get_account, get_positions, list_accounts
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.output.accounts import (
    render_account,
    render_accounts,
    render_positions,
)
from schwab_cli.output.format import FormatError, pick_format
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


def _resolve_format(json: bool, md: bool):
    try:
        return pick_format(json, md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


def _handle_api_error(e: Exception) -> None:
    msg = str(e) if str(e) else type(e).__name__
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def run_list(*, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        data = list_accounts(client)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_accounts(data, fmt))


def run_show(account_number: str, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        data = get_account(client, account_number)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_account(data, fmt))


def run_positions(account_number: str | None, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        rows = get_positions(client, account_number)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_positions(rows, fmt))
