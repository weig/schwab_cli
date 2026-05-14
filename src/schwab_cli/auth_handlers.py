"""Handlers that capture the OAuth ``code`` (or, in the future, full
token bundles) after the user finishes login in their browser.

Today two concrete implementations ship:

* :class:`UserInputHandler` — prompts on stderr, reads one line from
  stdin, parses it as a bare code / querystring / full callback URL.
* :class:`CodeRelayHandler` — long-polls a relay endpoint that captured
  the OAuth callback in the user's browser.

Both run concurrently in :mod:`schwab_cli.auth_flows`; the first one to
produce a valid :data:`AuthResult` wins.

The :class:`AuthHandler` Protocol + :data:`AuthResult` union are the
extension seam for a future ``AuthServerHandler`` that returns a fully
exchanged token bundle (``TokenResult``).
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.parse
from typing import Literal, Protocol, TypedDict

import httpx
import typer


class CodeResult(TypedDict):
    """OAuth ``code`` waiting to be exchanged for tokens."""

    kind: Literal["code"]
    code: str
    state: str | None  # None when input was a bare code (state unverifiable)


class TokenResult(TypedDict):
    """Already-exchanged token bundle. Returned by a future ``AuthServerHandler``."""

    kind: Literal["token"]
    access_token: str
    refresh_token: str
    expires_in: int


AuthResult = CodeResult | TokenResult


class AuthHandlerError(Exception):
    """Raised when a handler cannot produce a usable :data:`AuthResult`."""


class AuthHandler(Protocol):
    """Structural type for handlers consumed by the auth-flow race.

    ``cancel`` is optional so handlers can be unit-tested without a race
    harness. When set, handlers should bail at the next safe point.
    """

    def wait_for_response(
        self,
        *,
        expected_state: str,
        cancel: threading.Event | None = None,
    ) -> AuthResult: ...


# --------------------------------------------------------------------- #
# UserInputHandler                                                       #
# --------------------------------------------------------------------- #


_USER_PROMPT = (
    "Paste the code, the querystring (code=...&state=...), or the full "
    "redirect URL.\n(If you have a relay server set up, the code will be "
    "retrieved automatically.)\n> "
)


class UserInputHandler:
    """Read one line of input and parse it as code / querystring / URL.

    Cancellation note: once :func:`sys.stdin.readline` is blocking on the
    terminal, Python cannot interrupt it from another thread. This
    handler is meant to run on a daemon thread in
    :mod:`schwab_cli.auth_flows`; if another handler wins the race, the
    process exits and the orphaned read dies with it.
    """

    def wait_for_response(
        self,
        *,
        expected_state: str,
        cancel: threading.Event | None = None,
    ) -> AuthResult:
        typer.echo(_USER_PROMPT, err=True, nl=False)
        # Read one line. ``input()`` would write the prompt to stdout and
        # consume EOF as EOFError; ``sys.stdin.readline`` returns "" on
        # EOF which we treat as empty input.
        line = sys.stdin.readline()
        if cancel is not None and cancel.is_set():
            raise AuthHandlerError("cancelled before parse")
        raw = line.strip()
        if not raw:
            raise AuthHandlerError("empty input")
        return _parse_user_input(raw, expected_state=expected_state)


def _parse_user_input(raw: str, *, expected_state: str) -> CodeResult:
    """Sniff input shape and parse out (code, state).

    Recognized shapes, in order:
      1. Full URL (contains ``://``) — parse the query component.
      2. Querystring fragment (contains ``code=``).
      3. Bare code (anything else).

    For shapes 1 and 2, ``state=`` is verified against ``expected_state``
    when present. Shape 3 cannot verify state and emits a stderr warning.
    """
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        return _from_querystring(parsed.query, expected_state=expected_state)
    # Querystring shape: presence of any ``key=value`` pair. Schwab codes
    # don't contain ``=``, so this won't false-positive on a bare code.
    if "=" in raw:
        return _from_querystring(raw, expected_state=expected_state)
    # Bare code path. State cannot be verified.
    typer.echo(
        "warning: state verification skipped (you pasted a bare code). "
        "This is OK if you trust the channel you copied it from.",
        err=True,
    )
    return {"kind": "code", "code": raw, "state": None}


def _from_querystring(query: str, *, expected_state: str) -> CodeResult:
    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    if "error" in params:
        desc = params.get("error_description") or ""
        suffix = f": {desc}" if desc else ""
        raise AuthHandlerError(
            f"Schwab returned OAuth error '{params['error']}'{suffix}"
        )
    code = params.get("code")
    if not code:
        raise AuthHandlerError(
            "input did not contain a 'code' value — paste the URL "
            "or querystring from the redirect page."
        )
    state = params.get("state")
    if state is not None and state != expected_state:
        raise AuthHandlerError(
            "OAuth state mismatch — possible CSRF or stale callback. "
            "Restart the auth flow."
        )
    return {"kind": "code", "code": code, "state": state}


# --------------------------------------------------------------------- #
# CodeRelayHandler                                                       #
# --------------------------------------------------------------------- #


# Per-poll connection budget — must exceed the relay's long-poll window.
_POLL_HTTP_TIMEOUT_SECONDS = 40

# Default wall-clock budget for the whole race contribution. The browser
# is the slow part of the flow; the relay should answer within a second
# of the user finishing login. Anything beyond ~30s usually means the
# relay is broken or the user is stuck.
_DEFAULT_DEADLINE_SECONDS = 30.0


class CodeRelayHandler:
    """Long-poll a relay URL that captured the OAuth ``?code=...`` callback.

    Preserves the wire protocol from the prior implementation:
      * 200 + querystring body → return parsed code/state.
      * 408                    → loop (relay's long-poll window expired).
      * httpx.ReadTimeout      → loop.
      * 403                    → fatal (relay rejected request).
      * other                  → fatal.

    Cancellation: checks ``cancel.is_set()`` before each poll iteration.
    """

    def __init__(
        self,
        relay_url: str,
        *,
        deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
        http_timeout_seconds: float = _POLL_HTTP_TIMEOUT_SECONDS,
    ):
        self._relay_url = relay_url
        self._deadline_seconds = deadline_seconds
        self._http_timeout_seconds = http_timeout_seconds

    def wait_for_response(
        self,
        *,
        expected_state: str,
        cancel: threading.Event | None = None,
    ) -> AuthResult:
        deadline = time.time() + self._deadline_seconds
        while True:
            if cancel is not None and cancel.is_set():
                raise AuthHandlerError("cancelled by other handler")
            if time.time() >= deadline:
                raise AuthHandlerError(
                    f"relay did not return a code within "
                    f"{self._deadline_seconds:.0f}s"
                )
            try:
                resp = httpx.get(
                    self._relay_url, timeout=self._http_timeout_seconds,
                )
            except httpx.ReadTimeout:
                continue
            except httpx.RequestError as e:
                raise AuthHandlerError(
                    f"relay request failed: {type(e).__name__}: {e}"
                ) from e

            if resp.status_code == 200:
                return _from_querystring(
                    resp.text, expected_state=expected_state,
                )
            if resp.status_code == 408:
                continue
            if resp.status_code == 403:
                raise AuthHandlerError(
                    "Relay rejected request (403). Verify code_relay_url "
                    "and the matching redirect_uri are correct."
                )
            raise AuthHandlerError(
                f"Relay returned unexpected status {resp.status_code}: "
                f"{resp.text[:200]}"
            )
