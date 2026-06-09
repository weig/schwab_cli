"""Handlers and subprocess helpers for the OAuth ``code`` capture flow.

The concrete handler that ships here is :class:`UserInputHandler` — it
prompts on stderr, reads one line from stdin, and parses it as a bare
code / querystring / full callback URL. It is the paste fallback that
always joins the race in :mod:`schwab_cli.auth_flows`. The loopback
``local_server`` handler lives in :mod:`schwab_cli.auth_callback_server`.

This module also hosts the auto-login subprocess plumbing used by the
``local_server`` flow when an ``auto_login_command`` is configured:
:class:`AutoLoginSupervisor` (keeps webauto-cli alive while the local
callback server captures the redirect), :func:`_build_webauto_argv`, and
:func:`_terminate`.

The :class:`AuthHandler` Protocol + :data:`AuthResult` union are the
extension seam for a future ``AuthServerHandler`` that returns a fully
exchanged token bundle (``TokenResult``).

OAuth-level errors (``?error=access_denied&...``) are surfaced as
:class:`ErrorResult` values, NOT as exceptions. The race short-circuits
on ``ErrorResult`` and :func:`schwab_cli.oauth.resolve_auth_result`
converts them to :class:`OAuthAuthorizationError`. Operational failures
(network, state mismatch, parse error) remain as
:class:`AuthHandlerError` exceptions — the race keeps going so other
handlers can still succeed.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
import urllib.parse
from typing import Literal, Protocol, TypedDict

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


class ErrorResult(TypedDict):
    """OAuth error response from the IdP (e.g. ``?error=access_denied&...``).

    Authoritative answer from the OAuth provider, not a handler-internal
    failure. The race short-circuits on ``ErrorResult`` the same way it
    does on ``CodeResult`` / ``TokenResult``. Surfaced by
    :func:`schwab_cli.oauth.resolve_auth_result` as
    ``OAuthAuthorizationError``.
    """

    kind: Literal["error"]
    error: str
    error_description: str | None
    state: str | None


AuthResult = CodeResult | TokenResult | ErrorResult


class AuthHandlerError(Exception):
    """Raised on operational failures (handler crash, network error, state
    mismatch, etc.). NOT used for OAuth-level errors — those flow as
    :class:`ErrorResult` values via the race."""


class StaleCallbackError(AuthHandlerError):
    """A callback whose ``state`` doesn't match the one we generated for
    this attempt — i.e. a leftover from a prior auth flow.

    Subclasses :class:`AuthHandlerError` so the paste path (which has no
    way to know whether a mismatch is stale vs. hostile) keeps treating
    it as a hard error and tells the user. The relay path, by contrast,
    catches this specifically and keeps polling: the relay deletes on
    read, so a stale entry is drained and our real callback arrives on a
    later poll. CSRF protection is unchanged — a mismatched state is
    never *accepted*, only (in the relay case) skipped."""


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
    "redirect URL.\n(If the local callback server captures the redirect "
    "first, this is retrieved automatically.)\n> "
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


def _parse_user_input(
    raw: str, *, expected_state: str,
) -> CodeResult | ErrorResult:
    """Sniff input shape and parse out a ``CodeResult`` or ``ErrorResult``.

    Recognized shapes, in order:
      1. Full URL (contains ``://``) — parse the query component.
      2. Querystring fragment (contains a ``key=value`` pair).
      3. Bare code (anything else).

    For shapes 1 and 2, ``state=`` is verified against ``expected_state``
    when present and the OAuth ``error`` parameter is mapped to
    :class:`ErrorResult`. Shape 3 always produces a ``CodeResult`` with
    ``state=None`` and emits a stderr warning that state was not verified.
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


def _from_querystring(
    query: str, *, expected_state: str,
) -> CodeResult | ErrorResult:
    """Parse an OAuth callback querystring.

    Returns:
        * :class:`CodeResult` when ``code=`` is present and state validates.
        * :class:`ErrorResult` when ``error=`` is present and state validates
          (state on error responses is permissive — accepted when missing).

    Raises:
        :class:`AuthHandlerError` on operational failure (no recognizable
        ``code`` or ``error`` field, state mismatch).
    """
    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    state = params.get("state")
    if "error" in params:
        # State on error responses: strict if present, permissive if absent.
        # RFC 6749 §4.1.2.1 says providers SHOULD echo state in error
        # responses, but some skip it — accept missing rather than reject.
        if state is not None and state != expected_state:
            raise StaleCallbackError(
                "OAuth state mismatch on error response — possible CSRF or "
                "stale callback. Restart the auth flow."
            )
        return {
            "kind": "error",
            "error": params["error"],
            "error_description": params.get("error_description") or None,
            "state": state,
        }
    code = params.get("code")
    if not code:
        raise AuthHandlerError(
            "input did not contain a 'code' or 'error' value — paste the "
            "URL or querystring from the redirect page."
        )
    if state is not None and state != expected_state:
        raise StaleCallbackError(
            "OAuth state mismatch — possible CSRF or stale callback. "
            "Restart the auth flow."
        )
    return {"kind": "code", "code": code, "state": state}


# --------------------------------------------------------------------- #
# Subprocess helpers (shared by AutoLoginSupervisor)                     #
# --------------------------------------------------------------------- #


import subprocess
from pathlib import Path

_TERMINATE_GRACE_SECONDS = 5.0


# --------------------------------------------------------------------- #
# AutoLoginSupervisor  (side-effect for auth_flow="local_server")        #
# --------------------------------------------------------------------- #


class AutoLoginSupervisor:
    """Spawn an external auto-login subprocess for the ``local_server`` flow.

    Not a race participant — the loopback ``LocalServerHandler`` captures
    the redirect; this supervisor just keeps the browser-driving
    subprocess alive while that happens. The subprocess is invoked in
    ``--no-notify`` mode so it doesn't try to POST a notification.

    Caller (``auth_flows.get_auth_response``) calls :meth:`start` before
    the race, :meth:`terminate` in the outer ``finally:``.
    """

    def __init__(
        self,
        base_command: tuple[str, ...] | list[str],
        *,
        auth_url: str,
        state: str,
        stderr_log_dir: Path,
        timeout_seconds: float = 300.0,
    ):
        self._base_command = tuple(base_command)
        self._auth_url = auth_url
        self._state = state
        self._stderr_log_dir = Path(stderr_log_dir)
        self._timeout_seconds = timeout_seconds
        self._proc: subprocess.Popen | None = None
        self._watchdog: threading.Timer | None = None
        # Guards terminate()'s read-clear of _watchdog/_proc: the watchdog
        # Timer fires on its own thread and may race the caller's finally.
        self._lock = threading.Lock()

    def start(self) -> None:
        argv, _log_path = _build_webauto_argv(
            base_command=self._base_command,
            auth_url=self._auth_url,
            extra_flags=[
                "--no-notify",
                "--state", self._state,
            ],
            stderr_log_dir=self._stderr_log_dir,
        )
        stderr = None if _debug_enabled() else subprocess.DEVNULL
        # start_new_session=True puts the subprocess in its own session and
        # process group, so terminate() can ``killpg`` the whole tree —
        # webauto-cli → seleniumbase → uc_driver → chrome — instead of
        # orphaning the browser-driver grandchildren (which otherwise leak).
        self._proc = subprocess.Popen(  # noqa: S603 — argv from cfg, not shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=stderr,
            start_new_session=True,
        )
        # Watchdog kills the subprocess after timeout even if the caller
        # forgets to call terminate().
        self._watchdog = threading.Timer(self._timeout_seconds, self.terminate)
        self._watchdog.daemon = True
        self._watchdog.start()

    def terminate(self) -> None:
        """SIGTERM → 5s → SIGKILL. Idempotent and thread-safe.

        The watchdog Timer fires on its own thread and can race the
        caller's ``finally``. We atomically capture-and-clear both
        references under the lock, then run the side effects (``cancel``,
        ``_terminate``) outside it so we never hold the lock across the
        multi-second subprocess teardown. Whichever caller wins gets the
        non-None references; the loser sees ``None`` and no-ops.
        """
        with self._lock:
            watchdog, self._watchdog = self._watchdog, None
            proc, self._proc = self._proc, None
        if watchdog is not None:
            watchdog.cancel()
        if proc is not None:
            _terminate(proc)


# --------------------------------------------------------------------- #
# Shared subprocess helpers                                              #
# --------------------------------------------------------------------- #


def _debug_enabled() -> bool:
    """``DEBUG`` env in {1, true, yes} (case-insensitive)."""
    return os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def _build_webauto_argv(
    *,
    base_command: tuple[str, ...] | list[str],
    auth_url: str,
    extra_flags: list[str],
    stderr_log_dir: Path,
) -> tuple[list[str], Path | None]:
    """Build the webauto-cli argv with the common-tail flags.

    Returns ``(argv, log_path)``. When ``DEBUG`` is set we append
    ``--log <log_path>`` so webauto writes a structured log (and tees to
    its stderr — which we let flow through to the user's terminal since
    there's no paste prompt to compete with). When DEBUG is off, we omit
    ``--log`` entirely; webauto runs quiet and ``log_path`` is ``None``.

    The ``-a URL=...`` passthrough always goes last so it overrides any
    ``URL=`` baked into the user's ``--env`` file.
    """
    log_path: Path | None = None
    log_flags: list[str] = []
    if _debug_enabled():
        stderr_log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        log_path = stderr_log_dir / f"auto_login-{ts}.stderr.log"
        log_flags = ["--log", str(log_path)]

    argv = [
        *base_command,
        *extra_flags,
        *log_flags,
        "-a", f"URL={auth_url}",
    ]
    return argv, log_path


def _terminate(proc: subprocess.Popen) -> None:
    """Tear down the subprocess and its whole tree. Idempotent.

    The subprocess is spawned with ``start_new_session=True`` (see
    :meth:`AutoLoginSupervisor.start`), so it leads its own process group.
    We signal that group with ``killpg`` — SIGTERM, 5s grace, then a
    SIGKILL sweep — so the browser-driver descendants (seleniumbase →
    uc_driver → chrome) go down with it instead of being orphaned and
    leaked. The final SIGKILL is unconditional (any already-dead group
    yields ``ProcessLookupError``, which we swallow) so a child that
    ignores SIGTERM can't survive the leader's exit. (A descendant forked
    *into* the group during the grace window — after SIGKILL enumerates
    members — could still escape; webauto's driver tree is spun up before
    teardown, not during, so this is not a concern in practice.)

    Defensive: if the child is *not* its own group leader (spawned without
    ``start_new_session``), we fall back to single-process signals — never
    ``killpg`` a shared group that would also hit the daemon itself.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    if pgid != proc.pid:
        _terminate_single(proc)
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _terminate_single(proc: subprocess.Popen) -> None:
    """SIGTERM, 5s grace, SIGKILL — for a single process only.

    Used when the child does not lead its own process group, so we must
    not ``killpg`` (it would signal the daemon's group too).
    """
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
