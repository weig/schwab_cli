"""Handlers that capture the OAuth ``code`` (or an OAuth ``error``, or in
the future a full token bundle) after the user finishes login in their
browser.

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

OAuth-level errors (``?error=access_denied&...``) are surfaced as
:class:`ErrorResult` values, NOT as exceptions. The race short-circuits
on ``ErrorResult`` and :func:`schwab_cli.oauth.resolve_auth_result`
converts them to :class:`OAuthAuthorizationError`. Operational failures
(network, state mismatch, parse error) remain as
:class:`AuthHandlerError` exceptions — the race keeps going so other
handlers can still succeed.
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
                try:
                    return _from_querystring(
                        resp.text, expected_state=expected_state,
                    )
                except StaleCallbackError:
                    # Relay served a callback from a prior attempt (e.g.
                    # a browser profile that restored the old callback
                    # tab and replayed its ?code=). The relay deletes on
                    # read, so that stale entry is now gone — keep
                    # polling for OUR callback instead of aborting the
                    # whole flow. The deadline still bounds the loop.
                    continue
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


# --------------------------------------------------------------------- #
# HttpNotificationListener                                               #
# --------------------------------------------------------------------- #


import http.server
import json
import queue
import secrets
import subprocess
from pathlib import Path


class HttpNotificationListener:
    """One-shot HTTP listener for an external auto-login subprocess.

    Binds ``http.server.HTTPServer`` to ``127.0.0.1:0`` (ephemeral port).
    The endpoint URL embeds an 8-hex-char random token in the path segment
    so a localhost port-scanner that probes ``/``, ``/oauth``, or any other
    path gets a ``404`` — it learns nothing about the listener's purpose.

    Wire protocol (single POST):
      * ``Content-Type: application/json``.
      * Body must include ``"kind"``: ``"code"``, ``"token"``, or ``"error"``.
      * Body must include ``"state"`` matching ``expected_state`` (for the
        code/error variants; token variant doesn't need state echo).
      * On accept: respond ``200 {"ok": true}`` and shut down.
      * On bad shape / state mismatch: respond ``400 {"error": "..."}`` and
        keep listening (so a later legit POST can still win).
      * Wrong method on the right path: ``405``.
      * Any other path: ``404`` (no body).
    """

    def __init__(self) -> None:
        self._token = secrets.token_hex(4)
        self._result_q: queue.Queue = queue.Queue()
        # Closure into the handler so it can push into the queue + read
        # the path-match secret without subclassing HTTPServer.
        listener_token = self._token
        result_q = self._result_q

        class _Handler(http.server.BaseHTTPRequestHandler):
            # Silence the default access log; we capture failures via
            # response codes instead.
            def log_message(self, format, *args):  # noqa: A003 — stdlib name
                return

            def _send_json(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_empty(self, status: int) -> None:
                """Send an empty-body response with explicit Content-Length 0.

                Without ``Content-Length``, some clients see TCP reset on
                connection close — explicit zero lets them shut down cleanly.
                """
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # noqa: N802 — stdlib name
                self._send_empty(405)

            def do_POST(self):  # noqa: N802 — stdlib name
                if self.path != f"/oauth/{listener_token}":
                    self._send_empty(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    self._send_json(400, {"error": f"malformed JSON: {e}"})
                    return
                if not isinstance(payload, dict) or "kind" not in payload:
                    self._send_json(400, {"error": "missing 'kind'"})
                    return
                # Surface the payload back to wait(); state validation
                # happens there so the listener can keep running on bad
                # shape but bail on state mismatch.
                result_q.put(payload)
                self._send_json(200, {"ok": True})

        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="AutoLoginListener",
            daemon=True,
        )
        self._thread.start()
        self._closed = False

    @property
    def transport_type(self) -> Literal["http"]:
        return "http"

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}/oauth/{self._token}"

    def wait(
        self,
        *,
        expected_state: str,
        deadline: float | None,
        cancel: threading.Event | None,
    ) -> AuthResult:
        """Block until a valid POST arrives, the deadline fires, or
        ``cancel`` is set.

        Returns the parsed :class:`AuthResult`. Raises :class:`AuthHandlerError`
        on timeout, cancellation, state mismatch, or unrecognised payload
        shape.
        """
        poll_interval = 0.2  # seconds between queue polls
        while True:
            if cancel is not None and cancel.is_set():
                self.close()
                raise AuthHandlerError("cancelled by other handler")
            if deadline is not None and time.time() >= deadline:
                self.close()
                raise AuthHandlerError("auto-login listener timed out")
            try:
                payload = self._result_q.get(timeout=poll_interval)
            except queue.Empty:
                continue
            # Validate; on bad shape, keep waiting (a later legit POST can
            # still win).
            result = _validate_payload(payload, expected_state=expected_state)
            if result is None:
                continue
            self.close()
            return result

    def close(self) -> None:
        """Stop the server and release the port. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # `shutdown()` must be called from a different thread than the
        # one serving.
        threading.Thread(target=self._server.shutdown, daemon=True).start()
        try:
            self._server.server_close()
        except OSError:
            pass


def _validate_payload(payload: dict, *, expected_state: str) -> AuthResult | None:
    """Map a parsed JSON POST body to a typed :class:`AuthResult` (or
    raise on hard failures, return None on recoverable bad shape).

    Returns:
        * An :class:`AuthResult` (code/token/error variant) when the body
          validates fully.
        * ``None`` when the body is recognisable but missing fields the
          listener should ignore (treat as if no POST happened — keep
          waiting for a later valid one).

    Raises:
        :class:`AuthHandlerError` on state mismatch (authoritative
        rejection — race contribution fails).
    """
    kind = payload.get("kind")
    state = payload.get("state")
    if kind == "code":
        code = payload.get("code")
        if not code:
            return None  # bad shape, keep waiting
        if state is not None and state != expected_state:
            raise AuthHandlerError(
                "OAuth state mismatch on notification POST — possible "
                "CSRF, stale callback, or wrong listener."
            )
        return {"kind": "code", "code": code, "state": state}
    if kind == "error":
        err = payload.get("error")
        if not err:
            return None
        if state is not None and state != expected_state:
            raise AuthHandlerError(
                "OAuth state mismatch on error notification."
            )
        return {
            "kind": "error",
            "error": err,
            "error_description": payload.get("error_description") or None,
            "state": state,
        }
    if kind == "token":
        # Token variant doesn't need state echo — the upstream did the
        # whole OAuth dance and there's no externally-visible 'code' to
        # confuse with another flow.
        for field in ("access_token", "refresh_token", "expires_in"):
            if field not in payload:
                return None  # bad shape, keep waiting
        try:
            expires_in = int(payload["expires_in"])
        except (TypeError, ValueError):
            return None
        return {
            "kind": "token",
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_in": expires_in,
        }
    # Unknown kind — keep waiting; a later legit POST can still win.
    return None


# --------------------------------------------------------------------- #
# AutoLoginHandler  (race participant for auth_flow="client")            #
# --------------------------------------------------------------------- #


_TERMINATE_GRACE_SECONDS = 5.0


class AutoLoginHandler:
    """Spawn an external auto-login subprocess and race for its result.

    For ``auth_flow="client"``: schwab_cli stands up a local HTTP listener
    and runs the configured ``auto_login_command`` (typically a
    ``webauto-cli`` invocation) in "remote mode", appending::

        --notification-endpoint <listener URL>
        --state <expected_state>
        --log <stderr_log_dir>/auto_login-<ts>.stderr.log
        -a URL=<auth_url>

    webauto-cli's ``--log`` flag writes the subprocess's stderr to that
    file (and continues to tee to the CLI's own stderr). schwab_cli no
    longer needs an inner stderr-draining thread.

    The subprocess's action script POSTs an :data:`AuthResult` to the
    listener when the redirect URL contains the OAuth response.

    Lifecycle (D5 — single cleanup point in a ``finally:`` block):

      1. Listener stands up.
      2. ``subprocess.Popen(..., stdin=DEVNULL, stderr=None)``.
         (Inherited terminal stderr — webauto writes its own log via ``--log``.)
      3. ``listener.wait(...)`` blocks until POST / deadline / cancel /
         subprocess-died.
      4. ``finally:`` SIGTERM → 5s → SIGKILL → ``listener.close()``.
    """

    def __init__(
        self,
        base_command: tuple[str, ...] | list[str],
        *,
        auth_url: str,
        stderr_log_dir: Path,
        timeout_seconds: float = 300.0,
        listener: HttpNotificationListener | None = None,
    ):
        self._base_command = tuple(base_command)
        self._auth_url = auth_url
        self._stderr_log_dir = Path(stderr_log_dir)
        self._timeout_seconds = timeout_seconds
        self._listener_override = listener

    def wait_for_response(
        self,
        *,
        expected_state: str,
        cancel: threading.Event | None = None,
    ) -> AuthResult:
        listener = self._listener_override or HttpNotificationListener()
        argv, log_path = _build_webauto_argv(
            base_command=self._base_command,
            auth_url=self._auth_url,
            extra_flags=[
                "--notification-endpoint", listener.endpoint,
                "--state", expected_state,
            ],
            stderr_log_dir=self._stderr_log_dir,
        )
        # Stderr disposition follows DEBUG: when --log was added (DEBUG=1),
        # let webauto's tee flow through to schwab_cli's terminal so the
        # user sees what webauto is doing. When --log was skipped (quiet
        # mode), drop stderr entirely.
        stderr = None if _debug_enabled() else subprocess.DEVNULL
        proc = subprocess.Popen(  # noqa: S603 — argv from cfg, not shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=stderr,
        )
        deadline = time.time() + self._timeout_seconds
        try:
            return _wait_for_listener_or_proc_exit(
                listener=listener,
                proc=proc,
                expected_state=expected_state,
                deadline=deadline,
                cancel=cancel,
                log_path=log_path,
            )
        finally:
            _terminate(proc)
            listener.close()


def _wait_for_listener_or_proc_exit(
    *,
    listener: HttpNotificationListener,
    proc: subprocess.Popen,
    expected_state: str,
    deadline: float,
    cancel: threading.Event | None,
    log_path: Path | None,
) -> AuthResult:
    """Loop: poll the listener AND ``proc.poll()`` so we short-circuit when
    the subprocess dies without notifying.

    ``log_path`` is informational only — it's included in error messages
    so the user can ``cat`` the file. It's ``None`` when DEBUG is off
    (no log file was requested), in which case error messages suggest
    re-running with ``DEBUG=1``.
    """
    def _log_hint() -> str:
        if log_path is not None:
            return f"; see {log_path}"
        return " (re-run with DEBUG=1 for a log file)"

    poll_interval = 0.2
    while True:
        if cancel is not None and cancel.is_set():
            raise AuthHandlerError("cancelled by other handler")
        if time.time() >= deadline:
            raise AuthHandlerError(
                f"auto-login script timed out{_log_hint()}"
            )
        rc = proc.poll()
        if rc is not None:
            # Subprocess exited. Give the listener a moment in case a POST
            # is in-flight (TCP race).
            try:
                payload = listener._result_q.get(timeout=0.5)
            except queue.Empty:
                raise AuthHandlerError(
                    f"auto-login script exited rc={rc} without notifying"
                    f"{_log_hint()}"
                )
            result = _validate_payload(payload, expected_state=expected_state)
            if result is None:
                raise AuthHandlerError(
                    f"auto-login script exited rc={rc} with malformed "
                    f"notification{_log_hint()}"
                )
            return result
        # Subprocess still running — poll the listener for the next
        # interval.
        try:
            payload = listener._result_q.get(timeout=poll_interval)
        except queue.Empty:
            continue
        result = _validate_payload(payload, expected_state=expected_state)
        if result is None:
            continue
        return result


# --------------------------------------------------------------------- #
# AutoLoginSupervisor  (side-effect for auth_flow="code_relay")          #
# --------------------------------------------------------------------- #


class AutoLoginSupervisor:
    """Spawn an external auto-login subprocess for the ``code_relay`` flow.

    Not a race participant — :class:`CodeRelayHandler` polls the remote
    relay; this supervisor just keeps the browser-driving subprocess
    alive while that happens. The subprocess is invoked in
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
        self._proc = subprocess.Popen(  # noqa: S603 — argv from cfg, not shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=stderr,
        )
        # Watchdog kills the subprocess after timeout even if the caller
        # forgets to call terminate().
        self._watchdog = threading.Timer(self._timeout_seconds, self.terminate)
        self._watchdog.daemon = True
        self._watchdog.start()

    def terminate(self) -> None:
        """SIGTERM → 5s → SIGKILL. Idempotent."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None


# --------------------------------------------------------------------- #
# Shared subprocess helpers                                              #
# --------------------------------------------------------------------- #


def _debug_enabled() -> bool:
    """``DEBUG`` env in {1, true, yes} (case-insensitive)."""
    import os
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
    """SIGTERM, 5s grace, SIGKILL. Idempotent — no-op on already-exited."""
    if proc.poll() is not None:
        return
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
