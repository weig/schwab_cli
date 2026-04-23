"""`greeks` command — detailed greek view for a single option contract.

Accepts any of the ticker-resolver input forms (``NVDA260501C240``,
``NVDA  260501C00240000``, ``NVDA260501C240.0``). Under the hood, calls
Schwab's ``/chains`` endpoint filtered to the single strike + expiry +
side derived from the ticker, runs the existing chain response shaper,
picks out the matching contract, and hands it to
:mod:`schwab_cli.output.greeks` for rendering.
"""

from __future__ import annotations

from datetime import date

import typer

from schwab_cli import config as config_module
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.output.chains import shape_envelope
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.greeks import render_greeks
from schwab_cli.session import load as load_session
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


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
    contract_type = "CALL" if opt.type == "C" else "PUT"

    client = _client()
    try:
        raw = get_chain(
            client,
            ticker.underlying,
            contract_type=contract_type,
            strike=opt.strike,
            strike_count=1,
            from_date=expiry_date,
            to_date=expiry_date,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    chain = shape_envelope(raw)
    match = _pick_contract(chain.get("contracts") or [], opt.strike, opt.type)
    if match is None:
        typer.secho(
            f"No {contract_type} contract for {ticker.underlying} "
            f"{expiry_date.isoformat()} strike ${opt.strike:.2f}. "
            "Verify the expiry + strike exist.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    envelope = {
        "underlyingSymbol": ticker.underlying,
        "expiry": expiry_date.isoformat(),
        "dte": chain.get("dte"),
        "underlying": chain.get("underlying") or {},
        "contract": match,
    }
    typer.echo(render_greeks(envelope, fmt=fmt))


def _pick_contract(contracts: list[dict], strike: float, side: str) -> dict | None:
    """Choose the contract whose strike + side match the requested option.

    Schwab's ``strike`` filter is fuzzy (it returns the closest strikes, not
    an exact match), so we enforce exactness locally. `strike` comparison
    uses a small epsilon because strikes on exchange feeds are sometimes
    reported as floats with 3-decimal drift.
    """
    for c in contracts:
        if c.get("side") != side:
            continue
        cs = c.get("strike")
        if cs is None:
            continue
        if abs(cs - strike) < 1e-4:
            return c
    return None
