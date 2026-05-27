"""`greeks` command — detailed greek view for a single option contract.

Accepts any of the ticker-resolver input forms (``NVDA260501C240``,
``NVDA  260501C00240000``, ``NVDA260501C240.0``). Under the hood, the
service layer calls Schwab's ``/chains`` endpoint filtered to the single
strike + expiry + side derived from the ticker, runs the existing chain
response shaper, picks out the matching contract, and hands it to
:mod:`schwab_cli.output.greeks` for rendering.
"""

from __future__ import annotations

from datetime import date

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.greeks import render_greeks
from schwab_cli.service.greeks import get_greeks
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


@cli_errors
def run(ticker_raw: str, *, as_json: bool, as_md: bool) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        ticker = resolve_ticker(ticker_raw)
    except TickerError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if ticker.type != "option":
        typer.secho(
            f"{ticker_raw!r} is not an option ticker. "
            "Expected form like NVDA260501C240 or NVDA  260501C00240000.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    opt = ticker.option
    assert opt is not None  # narrowed by the check above
    expiry_date = date(int(opt.date[:4]), int(opt.date[4:6]), int(opt.date[6:8]))

    # ContractNotFound (a ServiceError carrying a complete message, exit 1) and
    # the auth / API errors are routed through the @cli_errors decorator.
    result = get_greeks(
        ticker.underlying,
        strike=opt.strike,
        expiry=expiry_date,
        side=opt.type,
    )

    typer.echo(render_greeks(result.envelope, fmt=fmt))
