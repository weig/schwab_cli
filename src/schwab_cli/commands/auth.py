"""``schwab_cli auth`` — refresh existing session, else open browser for fresh login."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli import oauth
from schwab_cli.auth_flows import AuthFlowError, get_auth_response
from schwab_cli.auth_handlers import AuthHandlerError
from schwab_cli.oauth import TokenResponse
from schwab_cli.session import Session, SessionError
from schwab_cli.session import save as save_session
from schwab_cli.session import load as load_session
from schwab_cli.utils import _summarize_error


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def run(force: bool, manual: bool = False) -> None:
    """Refresh-or-fresh auth orchestration.

    1. Load config; bail if missing or unreadable.
    2. Unless ``--force``: try to refresh the existing session. On success,
       save and exit.
    3. Otherwise (forced, or refresh failed): run ``get_auth_response()``
       — opens the user's default browser, races configured handlers,
       returns either a ``code`` (needs exchange) or a fully-exchanged
       token bundle.
    4. Save the resulting session.

    ``manual`` is a deprecated no-op kept for backward compatibility with
    older invocations. There is no longer an automated browser flow to
    opt out of.
    """
    del manual  # deprecated; flag kept in CLI for backward compat

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
        result = get_auth_response(cfg)
    except (AuthFlowError, AuthHandlerError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if result["kind"] == "code":
        try:
            tr = oauth.exchange_code(cfg, result["code"])
        except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
            typer.secho(
                f"Token exchange failed: {_summarize_error(e)}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
    else:  # "token" — handler already exchanged (future AuthServerHandler)
        tr = TokenResponse(
            access_token=result["access_token"],
            refresh_token=result["refresh_token"],
            expires_in=result["expires_in"],
        )

    new_session = Session.from_token_response(tr, now=int(time.time()))
    save_session(new_session)
    typer.secho(
        f"\nAuthenticated. Access token expires at {_iso(new_session.expires_at)}.",
        fg=typer.colors.GREEN,
    )
