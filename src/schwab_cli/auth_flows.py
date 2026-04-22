"""Auth-code retrieval flows.

Each flow returns the OAuth `code` query parameter that Schwab's authorize
endpoint hands back; the caller then exchanges it for tokens.

Flows:
- `local`        : Playwright/SeleniumBase drives a local browser and scrapes
                   the redirect URL out of the address bar.
- `code_relay`   : Open the user's default browser to the OAuth URL, poll a
                   pre-deployed Cloudflare Worker relay (long-poll) for the
                   captured callback, parse `code` out of the returned
                   querystring.
- `client`       : Open the user's default browser, then prompt them to copy
                   the redirected URL from the address bar and paste it back.

The `local` flow keeps its existing implementation in
`schwab_cli.browser.flow` to avoid pulling Playwright into the import path of
the other flows.
"""

from __future__ import annotations

import secrets
import time
import urllib.parse
import webbrowser

import httpx
import typer

from schwab_cli import oauth
from schwab_cli.config import Config


class AuthFlowError(Exception):
    """Raised when an auth flow fails to obtain a usable authorization code."""


# Total wall-clock budget the user has to complete the browser leg of OAuth.
_BROWSER_AUTH_TIMEOUT_SECONDS = 300

# Per-poll connection budget — must exceed the relay's long-poll window.
_POLL_HTTP_TIMEOUT_SECONDS = 40


def get_code(cfg: Config) -> str:
    """Run the auth flow named in `cfg.auth_flow` and return the `code`."""
    if cfg.auth_flow == "local":
        # Imported lazily so non-local flows don't pay the Playwright import cost.
        from schwab_cli.browser.flow import run_full_auth
        return run_full_auth(cfg)
    if cfg.auth_flow == "code_relay":
        return _code_relay_get_code(cfg)
    if cfg.auth_flow == "client":
        return _client_get_code(cfg)
    raise AuthFlowError(f"unknown auth_flow {cfg.auth_flow!r}")


def _code_relay_get_code(cfg: Config) -> str:
    if not cfg.code_relay_url:
        # The config loader normally catches this; defensive only.
        raise AuthFlowError("auth_flow='code_relay' requires code_relay_url")

    state = secrets.token_urlsafe(32)
    auth_url = oauth.build_auth_url(cfg, state=state)

    typer.echo("Opening browser for Schwab login...")
    typer.echo(f"  If it does not open automatically, visit:\n    {auth_url}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        # webbrowser.open returns False on failure rather than raising on most
        # platforms, but be defensive in case a backend raises.
        pass

    typer.echo("Waiting for callback (up to 5 min)...")
    deadline = time.time() + _BROWSER_AUTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            resp = httpx.get(cfg.code_relay_url, timeout=_POLL_HTTP_TIMEOUT_SECONDS)
        except httpx.ReadTimeout:
            continue
        except httpx.RequestError as e:
            raise AuthFlowError(
                f"relay request failed: {type(e).__name__}: {e}"
            ) from e

        if resp.status_code == 200:
            return _extract_code_from_query(resp.text, expected_state=state)
        if resp.status_code == 408:
            # Long-poll cycle ended with no callback; reconnect.
            continue
        if resp.status_code == 403:
            raise AuthFlowError(
                "Relay rejected request (403). Verify code_relay_url and the "
                "matching redirect_uri are correct."
            )
        raise AuthFlowError(
            f"Relay returned unexpected status {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    raise AuthFlowError("Timed out waiting for OAuth callback (5 min).")


def _client_get_code(cfg: Config) -> str:
    state = secrets.token_urlsafe(32)
    auth_url = oauth.build_auth_url(cfg, state=state)

    typer.echo("Open the following URL in your browser:")
    typer.echo(f"  {auth_url}")
    typer.echo("")
    typer.echo("After login, Schwab will redirect to your registered redirect URI.")
    typer.echo("The page may show 'site can't be reached' — that is expected.")
    typer.echo("Copy the FULL URL from your browser's address bar and paste it here.")
    typer.echo("")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    redirected_url = typer.prompt("Redirected URL").strip()
    parsed = urllib.parse.urlparse(redirected_url)
    if not parsed.query:
        raise AuthFlowError("Pasted URL has no query string.")
    return _extract_code_from_query(parsed.query, expected_state=state)


def _extract_code_from_query(querystring: str, *, expected_state: str) -> str:
    """Parse a callback querystring and extract `code`, verifying `state`."""
    params = dict(urllib.parse.parse_qsl(querystring, keep_blank_values=True))
    if "error" in params:
        desc = params.get("error_description") or ""
        suffix = f": {desc}" if desc else ""
        raise AuthFlowError(f"Schwab returned OAuth error '{params['error']}'{suffix}")
    received_state = params.get("state")
    if received_state != expected_state:
        raise AuthFlowError(
            "OAuth state mismatch — possible CSRF or stale callback. "
            "Restart the auth flow."
        )
    code = params.get("code")
    if not code:
        raise AuthFlowError("Callback querystring did not contain a `code` value.")
    return code
