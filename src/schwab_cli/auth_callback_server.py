"""A local HTTPS (or, in tests, HTTP) callback server that captures the
OAuth ``?code=...&state=...`` redirect in the user's browser.

This is the loopback callback server (formerly the remote relay): instead
of polling a remote endpoint, schwab_cli binds ``127.0.0.1`` directly and
the IdP redirects the browser straight back to us.

Threading model:

  * A plain single-threaded :class:`http.server.HTTPServer` serves on a
    daemon thread, so requests are serialised and a plain boolean capture
    flag guarded by the GIL is safe.
  * The request handler NEVER calls ``self.server.shutdown()`` (that would
    deadlock the serving thread). Captures are pushed onto a queue;
    :meth:`CallbackServer.wait` (a different thread) drains the queue and
    performs teardown.
  * :meth:`CallbackServer.close` runs ``shutdown()`` on a separate thread
    and is idempotent.
"""
from __future__ import annotations

import http.server
import queue
import ssl
import threading
import time
import urllib.parse
from pathlib import Path

from schwab_cli.auth_handlers import (
    AuthHandlerError,
    AuthResult,
    CodeResult,
    ErrorResult,
    StaleCallbackError,
)
from schwab_cli.redirect_uri import parse_callback_uri

# Default wall-clock budget for the local-server race contribution. The
# browser round-trip is the slow part; once the user finishes login the
# redirect lands within a fraction of a second.
_DEFAULT_DEADLINE_SECONDS = 300.0

# Hosts for which a plaintext-http callback is tolerated (test-only switch).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_SUCCESS_BODY = (
    b"<html><body><h1>Login successful</h1>"
    b"<p>The authorization code was received. You can close this tab.</p>"
    b"</body></html>"
)
_ALREADY_BODY = (
    b"<html><body><h1>Already received</h1>"
    b"<p>The authorization code was already received. "
    b"You can close this tab.</p></body></html>"
)
_WAITING_BODY = (
    b"<html><body><h1>Still waiting</h1>"
    b"<p>Waiting for the authorization callback. You can close this tab.</p>"
    b"</body></html>"
)
_BAD_REQUEST_BODY = (
    b"<html><body><h1>Bad request</h1>"
    b"<p>The callback did not contain a code or error.</p></body></html>"
)


class CallbackServerError(Exception):
    """Raised on construction failures (missing cert, port in use)."""


def parse_callback_query(
    query: str, *, expected_state: str, require_state: bool
) -> CodeResult | ErrorResult:
    """Parse an OAuth callback querystring into a typed result.

    Mirrors :func:`schwab_cli.auth_handlers._from_querystring` with an
    added ``require_state`` switch:

      * Error branch: a present-but-mismatched ``state`` raises
        :class:`StaleCallbackError`; a missing ``state`` is permissive
        (RFC 6749 providers SHOULD echo state on errors but may omit it),
        regardless of ``require_state``.
      * Code branch with ``require_state=True``: a missing OR mismatched
        ``state`` raises :class:`StaleCallbackError`.
      * Code branch with ``require_state=False``: a missing ``state`` is
        accepted (``state=None``); a present-but-mismatched ``state`` still
        raises :class:`StaleCallbackError`.

    Raises :class:`AuthHandlerError` when neither ``code`` nor ``error`` is
    present.
    """
    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    state = params.get("state")

    if "error" in params:
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
            "callback did not contain a 'code' or 'error' value."
        )

    if state is None:
        if require_state:
            raise StaleCallbackError(
                "OAuth callback missing 'state' — refusing to accept a "
                "stateless callback. Restart the auth flow."
            )
        return {"kind": "code", "code": code, "state": None}

    if state != expected_state:
        raise StaleCallbackError(
            "OAuth state mismatch — possible CSRF or stale callback. "
            "Restart the auth flow."
        )
    return {"kind": "code", "code": code, "state": state}


class _CallbackHTTPServer(http.server.HTTPServer):
    """Single-threaded HTTPServer with port-sharing disabled.

    ``allow_reuse_address`` is ``True`` by default on some Python builds,
    which lets a second process bind the same port — exactly what we want
    to detect. Force it off so a busy port raises on bind.
    """

    allow_reuse_address = False


class CallbackServer:
    """Bind a loopback callback endpoint and capture the OAuth redirect."""

    def __init__(
        self,
        redirect_uri: str,
        *,
        certfile: str | Path | None = None,
        keyfile: str | Path | None = None,
    ) -> None:
        target = parse_callback_uri(redirect_uri)
        self._path = target.path

        if target.scheme == "https" and (certfile is None or keyfile is None):
            raise CallbackServerError(
                "https callback requires both a certfile and a keyfile."
            )

        # http is a test-only switch for loopback. Refuse plaintext to a
        # non-loopback host so a misconfigured redirect_uri never serves an
        # OAuth code in the clear. (Production requires https; that guard lives
        # in auth_flows/setup via redirect_uri.is_loopback_https.)
        if target.scheme == "http" and target.host not in _LOOPBACK_HOSTS:
            raise CallbackServerError(
                "refusing to serve an OAuth callback over plaintext http to a "
                f"non-loopback host ({target.host}); use https."
            )

        # Server-scoped capture state. A new handler instance is created per
        # request, so the flag/queue MUST live on the server, not the
        # handler. Requests are serialised (single-threaded server), so the
        # GIL-guarded boolean needs no extra lock.
        self._result_q: queue.Queue = queue.Queue()
        self._captured = False
        self._expected_state: str | None = None
        self._closed = False

        handler_cls = self._build_handler(target.path)

        try:
            self._server = _CallbackHTTPServer((target.host, target.port), handler_cls)
        except OSError as exc:
            raise CallbackServerError(
                f"port {target.port} in use; close the other process or "
                f"re-run setup"
            ) from exc

        if target.scheme == "https":
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
                self._server.socket = ctx.wrap_socket(
                    self._server.socket, server_side=True
                )
            except (OSError, ssl.SSLError) as exc:
                self._server.server_close()
                raise CallbackServerError(
                    f"failed to load TLS cert/key: {exc}"
                ) from exc

        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            # Tighter poll interval so close()'s synchronous shutdown() returns
            # promptly (default is 0.5s).
            target=lambda: self._server.serve_forever(poll_interval=0.1),
            name="CallbackServer",
            daemon=True,
        )
        self._thread.start()

    def _build_handler(self, path: str) -> type[http.server.BaseHTTPRequestHandler]:
        server = self  # closure for the capture state + expected_state

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 — stdlib name
                return

            def _send_html(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _reject_method(self) -> None:
                # Wrong path → 404 (reveal nothing); right path but wrong
                # method → 405.
                if urllib.parse.urlparse(self.path).path != path:
                    self._send_empty(404)
                else:
                    self._send_empty(405)

            do_POST = _reject_method  # noqa: N815 — stdlib name
            do_PUT = _reject_method  # noqa: N815 — stdlib name
            do_DELETE = _reject_method  # noqa: N815 — stdlib name
            do_PATCH = _reject_method  # noqa: N815 — stdlib name
            do_HEAD = _reject_method  # noqa: N815 — stdlib name

            def do_GET(self):  # noqa: N802 — stdlib name
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != path:
                    self._send_empty(404)
                    return

                expected_state = server._expected_state
                if expected_state is None:
                    # A GET arrived before wait() set the expected state.
                    # Treat as keep-waiting; do not capture.
                    self._send_html(200, _WAITING_BODY)
                    return

                try:
                    result = parse_callback_query(
                        parsed.query,
                        expected_state=expected_state,
                        require_state=True,
                    )
                except StaleCallbackError:
                    # Wrong/missing state — keep waiting, do not capture.
                    self._send_html(200, _WAITING_BODY)
                    return
                except AuthHandlerError:
                    # Neither code nor error — bad request, keep waiting.
                    self._send_html(400, _BAD_REQUEST_BODY)
                    return

                # Server-scoped, GIL-guarded idempotent capture.
                if server._captured:
                    self._send_html(200, _ALREADY_BODY)
                    return
                server._captured = True
                server._result_q.put(result)
                self._send_html(200, _SUCCESS_BODY)

        return _Handler

    @property
    def port(self) -> int:
        return self._port

    def wait(
        self,
        *,
        expected_state: str,
        deadline: float | None,
        cancel: threading.Event | None,
    ) -> AuthResult:
        """Block until a valid callback arrives, the deadline fires, or
        ``cancel`` is set.

        ``wait()`` is the source of truth for ``expected_state``: it stores
        it on the server so the handler can validate incoming GETs.
        """
        self._expected_state = expected_state
        poll_interval = 0.2
        while True:
            if cancel is not None and cancel.is_set():
                self.close()
                raise AuthHandlerError("cancelled by other handler")
            if deadline is not None and time.time() >= deadline:
                self.close()
                raise AuthHandlerError("local callback server timed out")
            try:
                result = self._result_q.get(timeout=poll_interval)
            except queue.Empty:
                continue
            # Do NOT close on success: the browser may re-issue the GET
            # (refresh, relay-vs-browser race) and should still get a benign
            # "already received" 200. Teardown is the caller's job (or the
            # race harness's outer finally) via close().
            return result

    def close(self) -> None:
        """Stop the server and release the port. Idempotent.

        ``close()`` is only ever called from the waiter/caller thread — never
        from the serving thread (the request handler never calls it, per H1) —
        so it can call ``shutdown()`` synchronously. ``shutdown()`` blocks until
        ``serve_forever`` has exited, and only THEN do we ``server_close()`` the
        socket: this avoids both the cross-thread deadlock (close ≠ serve
        thread) and the EBADF race (never close the socket while the serve loop
        still selects on it). On return the port is fully released.
        """
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        try:
            self._server.server_close()
        except OSError:
            pass


class LocalServerHandler:
    """:class:`schwab_cli.auth_handlers.AuthHandler` over a :class:`CallbackServer`."""

    def __init__(
        self,
        redirect_uri: str,
        *,
        certfile: str | Path | None = None,
        keyfile: str | Path | None = None,
        deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self._server = CallbackServer(
            redirect_uri, certfile=certfile, keyfile=keyfile
        )
        self._deadline_seconds = deadline_seconds

    def wait_for_response(
        self,
        *,
        expected_state: str,
        cancel: threading.Event | None = None,
    ) -> AuthResult:
        deadline = time.time() + self._deadline_seconds
        # ``wait()`` only self-closes on cancel/deadline; on the success path it
        # leaves the server live (so a near-simultaneous duplicate GET — browser
        # nav vs. CDP-watch relay — still gets a clean "already received"). The
        # handler owns teardown so the bound port is always released once the
        # race contribution is done. ``close()`` is idempotent.
        try:
            return self._server.wait(
                expected_state=expected_state,
                deadline=deadline,
                cancel=cancel,
            )
        finally:
            self._server.close()

    def close(self) -> None:
        """Release the bound port/thread. Idempotent."""
        self._server.close()
