from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli import oauth
from schwab_cli.auth_flows import AuthFlowError, get_code
from schwab_cli.browser._seleniumbase_flow import AuthError
from schwab_cli.utils import _summarize_error
from schwab_cli.secrets import SecretError
from schwab_cli.session import Session, SessionError
from schwab_cli.session import save as save_session
from schwab_cli.session import load as load_session


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def run(force: bool, manual: bool = False) -> None:
    # `--manual` turns off saved-credential automation and forces a visible
    # browser by setting HEADLESS=0 for the remainder of this invocation; the
    # user drives the Schwab login themselves. The configured auth_flow
    # (client / code_relay) still owns how the callback code is captured.
    if manual:
        os.environ["HEADLESS"] = "0"

    try:
        cfg = config_module.load()
    except config_module.ConfigError as e:
        typer.secho(
            f"Config is unusable: {e}\nRun `schwab_cli setup` to fix.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if not force:
        try:
            session = load_session()
        except SessionError as e:
            # A corrupt session file shouldn't block the user — treat as
            # "no session" and fall through to full auth to mint a new one.
            typer.echo(f"Stored session is unreadable ({e}); doing full auth.")
            session = None
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
        code = get_code(cfg, manual=manual)
    except (AuthError, AuthFlowError, SecretError) as e:
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
