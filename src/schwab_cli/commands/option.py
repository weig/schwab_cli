from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.option_spec import OptionSpecError, parse_option_spec
from schwab_cli.output.chains import render_chain, shape_envelope
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


def run(
    symbol: str,
    spec_str: str,
    *,
    strikes: int,
    detail: int,
    as_json: bool,
    as_md: bool,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        spec = parse_option_spec(spec_str)
    except OptionSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        # `kind` discriminator on OptionSpecError:
        #   "invalid"            -> grammar miss (exit 2)
        #   "bad_date"/"expired" -> date-level errors (exit 1)
        code = 2 if getattr(e, "kind", "invalid") == "invalid" else 1
        raise typer.Exit(code=code)

    client = _client()
    try:
        raw = get_chain(
            client,
            symbol.upper(),
            contract_type=spec.contract_type,
            strike=spec.strike,
            strike_count=strikes,
            from_date=spec.expiry,
            to_date=spec.expiry,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    envelope = shape_envelope(
        raw,
        # Trim only when no explicit strike was requested.
        strike_count=None if spec.strike is not None else strikes,
    )

    if not envelope["contracts"]:
        if spec.strike is not None:
            typer.secho(
                f"No contract at strike {spec.strike} for {symbol.upper()} "
                f"{spec.expiry.isoformat()}.",
                fg=typer.colors.RED,
                err=True,
            )
        else:
            typer.secho(
                f"No options found for {symbol.upper()} on {spec.expiry.isoformat()}.",
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(code=1)

    typer.echo(
        render_chain(
            envelope, fmt=fmt, detail=detail, requested_type=spec.contract_type
        )
    )
