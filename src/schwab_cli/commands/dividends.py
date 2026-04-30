from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.quotes import get_quotes
from schwab_cli.output.dividends import render_dividends
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.session import load as load_session
from schwab_cli.ticker import to_schwab_form


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

    # Normalize class-share separators (BRK.B → BRK/B) and case up front
    # so the renderer's per-symbol lookup matches the canonical keys
    # Schwab returns.
    symbols = [to_schwab_form(s) for s in symbols]

    client = _client()
    try:
        payload = get_quotes(client, symbols, fields="all")
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    upcoming_window = within_days if upcoming else None
    typer.echo(
        render_dividends(
            symbols, payload, fmt,
            upcoming_within_days=upcoming_window,
        )
    )
