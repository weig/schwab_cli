from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli import oauth
from schwab_cli.browser.flow import AuthError, run_full_auth
from schwab_cli.utils import _summarize_error
from schwab_cli.secrets import SecretError
from schwab_cli.session import Session
from schwab_cli.session import save as save_session
from schwab_cli.session import load as load_session


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def run(force: bool) -> None:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if not force:
        session = load_session()
        if session is not None:
            try:
                tr = oauth.refresh(cfg, session.refresh_token)
                new_session = Session.from_token_response(tr, now=int(time.time()))
                save_session(new_session)
                typer.secho(
                    f"Already logged in. Access token valid until {_iso(new_session.expires_at)}.",
                    fg=typer.colors.GREEN,
                )
                raise typer.Exit(code=0)
            except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
                typer.echo(
                    f"Refresh token rejected ({_summarize_error(e)}); doing full auth."
                )
                # fall through

    try:
        code = run_full_auth(cfg)
    except (AuthError, SecretError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        tr = oauth.exchange_code(cfg, code)
    except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
        typer.secho(
            f"Token exchange failed: {_summarize_error(e)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    new_session = Session.from_token_response(tr, now=int(time.time()))
    save_session(new_session)
    typer.secho(
        f"Authenticated. Access token expires at {_iso(new_session.expires_at)}.",
        fg=typer.colors.GREEN,
    )
