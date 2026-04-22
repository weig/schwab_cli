"""Auth-code retrieval flows.

Each flow returns the OAuth ``code`` query parameter that Schwab's authorize
endpoint hands back; the caller then exchanges it for tokens.

Two flows are supported:

- ``client``      : A SeleniumBase browser on this machine drives the login
                    (or waits for the user to drive it when ``manual=True``).
                    The authorization ``code`` is read out of the browser's
                    redirect to the loopback ``redirect_uri``.
- ``code_relay``  : Same browser session, but the configured ``redirect_uri``
                    is a pre-deployed relay endpoint. Once the browser hits
                    the relay, we long-poll ``code_relay_url`` to retrieve
                    the captured ``code``.

Browser visibility follows the ``HEADLESS`` env var for both flows. The
``--manual`` CLI flag sets ``HEADLESS=0`` and passes ``manual=True`` so the
user can drive the login in a visible window (e.g., when saved credentials
are missing or MFA is being rotated).
"""

from __future__ import annotations

import secrets
import time
import urllib.parse

import httpx

from schwab_cli.config import Config


class AuthFlowError(Exception):
    """Raised when an auth flow fails to obtain a usable authorization code."""


# Total wall-clock budget for polling the relay after the browser confirmed
# the redirect. The callback is already stored by that point, so this should
# return on the first poll unless the relay is flaky.
_RELAY_POLL_TIMEOUT_SECONDS = 30

# Per-poll connection budget — must exceed the relay's long-poll window.
_POLL_HTTP_TIMEOUT_SECONDS = 40


def get_code(cfg: Config, *, manual: bool = False) -> str:
    """Run the configured auth flow and return the OAuth ``code``.

    ``manual=False`` runs saved-credential automation; ``manual=True`` lets
    the user drive the Schwab login in a visible browser window.
    """
    if cfg.auth_flow == "client":
        return _client_get_code(cfg, manual=manual)
    if cfg.auth_flow == "code_relay":
        return _code_relay_get_code(cfg, manual=manual)
    raise AuthFlowError(f"unknown auth_flow {cfg.auth_flow!r}")


def _client_get_code(cfg: Config, *, manual: bool) -> str:
    """Loopback-redirect flow: parse ``code`` out of the browser's redirect URL."""
    from schwab_cli.browser._seleniumbase_flow import run_browser_auth

    state = secrets.token_urlsafe(32)
    redirect_url = run_browser_auth(cfg, automate=not manual, state=state)
    parsed = urllib.parse.urlparse(redirect_url)
    return _extract_code_from_query(parsed.query, expected_state=state)


def _code_relay_get_code(cfg: Config, *, manual: bool) -> str:
    """Relay flow: browser hits the relay's redirect; we poll the relay for the code."""
    from schwab_cli.browser._seleniumbase_flow import run_browser_auth

    if not cfg.code_relay_url:
        # The config loader normally catches this; defensive only.
        raise AuthFlowError("auth_flow='code_relay' requires code_relay_url")

    state = secrets.token_urlsafe(32)
    # Returned URL is unused; we trust the relay to have captured the callback.
    run_browser_auth(cfg, automate=not manual, state=state)
    return _poll_relay(cfg, expected_state=state)


def _poll_relay(cfg: Config, *, expected_state: str) -> str:
    assert cfg.code_relay_url is not None  # validated by caller
    deadline = time.time() + _RELAY_POLL_TIMEOUT_SECONDS
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
            return _extract_code_from_query(resp.text, expected_state=expected_state)
        if resp.status_code == 408:
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

    raise AuthFlowError(
        "Browser reached redirect but relay never returned the code (30s)."
    )


def _extract_code_from_query(querystring: str, *, expected_state: str) -> str:
    """Parse a callback querystring and extract ``code``, verifying ``state``."""
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
