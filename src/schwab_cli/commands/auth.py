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
       — opens the user's default browser, races configured handlers
       (paste fallback always; auto-login subprocess if configured;
       relay polling if configured), returns an ``AuthResult``.
    4. Hand the ``AuthResult`` to :func:`schwab_cli.oauth.resolve_auth_result`
       — the access-token layer that maps every variant (code / token /
       error) to a ``TokenResponse`` or surfaces an ``OAuthAuthorizationError``.
    5. Save the resulting session.

    ``manual=True`` skips the auto-login subprocess (if configured) and
    drives the race with just the paste fallback + any relay handler.
    """
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
                # refreshed_from: a refresh grant does NOT extend the
                # refresh token's life — preserve its true expiry
                # (from_token_response here would silently inflate it).
                new_session = Session.refreshed_from(
                    session, tr, now=int(time.time()),
                )
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
        result = get_auth_response(cfg, manual=manual)
    except (AuthFlowError, AuthHandlerError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        tr = oauth.resolve_auth_result(cfg, result)
    except oauth.OAuthAuthorizationError as e:
        typer.secho(
            f"\nOAuth error from Schwab: {e}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
        typer.secho(
            f"\nToken exchange failed: {_summarize_error(e)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    new_session = Session.from_token_response(tr, now=int(time.time()))
    save_session(new_session)
    typer.secho(
        f"\nAuthenticated. Access token expires at {_iso(new_session.expires_at)}.",
        fg=typer.colors.GREEN,
    )
