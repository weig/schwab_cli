"""Tests for `schwab_cli.auth_callback_server`.

Drives REAL localhost round-trips (http for most cases; https with a
tmp self-signed leaf from Phase 0 ``generate.py`` for the HTTPS test).

All tests are RED — they import from the not-yet-implemented module and
must fail with ``ModuleNotFoundError``.
"""
from __future__ import annotations

import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from schwab_cli.auth_callback_server import (
    CallbackServer,
    CallbackServerError,
    LocalServerHandler,
    parse_callback_query,
)
from schwab_cli.auth_handlers import (
    AuthHandlerError,
    CodeResult,
    ErrorResult,
    StaleCallbackError,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 and return the OS-assigned ephemeral port number.

    We bind, grab the port, then close immediately — there is a tiny
    TOCTOU window, but it is fine for tests on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_uri(port: int, path: str = "/cb") -> str:
    return f"http://127.0.0.1:{port}{path}"


def _wait_result_in_thread(
    server: CallbackServer,
    *,
    expected_state: str,
    deadline_offset: float = 5.0,
    cancel: threading.Event | None = None,
    out: dict,
) -> threading.Thread:
    """Run ``server.wait(...)`` in a background thread and store the result
    (or exception) in ``out`` so the test can assert on it after joining."""

    def _run():
        try:
            out["result"] = server.wait(
                expected_state=expected_state,
                deadline=time.time() + deadline_offset,
                cancel=cancel,
            )
        except Exception as exc:
            out["exc"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _tmp_leaf_cert_files(tmp_path: Path):
    """Generate a real CA + leaf pair (Phase 0 generate.py) and write them
    to tmp PEM files. Returns ``(certfile_path, keyfile_path)``."""
    from schwab_cli.cert.generate import cert_to_pem, generate_ca, generate_leaf, key_to_pem

    ca = generate_ca()
    leaf = generate_leaf(ca)
    certfile = tmp_path / "leaf.pem"
    keyfile = tmp_path / "leaf-key.pem"
    certfile.write_bytes(cert_to_pem(leaf.cert))
    keyfile.write_bytes(key_to_pem(leaf.key))
    return certfile, keyfile


# ---------------------------------------------------------------------
# parse_callback_query — unit tests
# ---------------------------------------------------------------------


def test_pcq_happy_path_code_with_matching_state():
    result = parse_callback_query(
        "code=ABC&state=S1", expected_state="S1", require_state=True
    )
    assert result == {"kind": "code", "code": "ABC", "state": "S1"}


def test_pcq_missing_state_strict_raises_stale_callback_error():
    """require_state=True: missing state must raise StaleCallbackError."""
    with pytest.raises(StaleCallbackError):
        parse_callback_query(
            "code=ABC", expected_state="S1", require_state=True
        )


def test_pcq_mismatched_state_strict_raises_stale_callback_error():
    """require_state=True: wrong state must raise StaleCallbackError."""
    with pytest.raises(StaleCallbackError):
        parse_callback_query(
            "code=ABC&state=WRONG", expected_state="S1", require_state=True
        )


def test_pcq_missing_state_permissive_accepted():
    """require_state=False: missing state accepted (parity with _from_querystring)."""
    result = parse_callback_query(
        "code=ABC", expected_state="S1", require_state=False
    )
    assert result["kind"] == "code"
    assert result["code"] == "ABC"
    assert result["state"] is None


def test_pcq_mismatched_state_permissive_raises_stale():
    """require_state=False: present-but-mismatched state still raises StaleCallbackError
    (mirrors _from_querystring behaviour — mismatch is always an error)."""
    with pytest.raises(StaleCallbackError):
        parse_callback_query(
            "code=ABC&state=WRONG", expected_state="S1", require_state=False
        )


def test_pcq_error_param_with_matching_state():
    result = parse_callback_query(
        "error=access_denied&state=S1", expected_state="S1", require_state=True
    )
    assert result == {
        "kind": "error",
        "error": "access_denied",
        "error_description": None,
        "state": "S1",
    }


def test_pcq_error_param_missing_state_permissive_on_errors():
    """RFC 6749: providers SHOULD echo state on errors but may omit it.
    For the error branch, missing state is always permissive (same as
    _from_querystring), regardless of require_state."""
    result = parse_callback_query(
        "error=access_denied", expected_state="S1", require_state=True
    )
    assert result["kind"] == "error"
    assert result["state"] is None


def test_pcq_error_with_description():
    result = parse_callback_query(
        "error=access_denied&error_description=user+rejected&state=S1",
        expected_state="S1",
        require_state=True,
    )
    assert result["error_description"] == "user rejected"


def test_pcq_no_code_no_error_raises_authhandlererror():
    """No code and no error → AuthHandlerError (not StaleCallbackError)."""
    with pytest.raises(AuthHandlerError) as exc_info:
        parse_callback_query("state=S1&foo=bar", expected_state="S1", require_state=True)
    assert not isinstance(exc_info.value, StaleCallbackError)


def test_pcq_empty_query_raises_authhandlererror():
    with pytest.raises(AuthHandlerError):
        parse_callback_query("", expected_state="S1", require_state=True)


def test_pcq_stale_callback_error_is_authhandlererror_subclass():
    """Contract: StaleCallbackError is a sub-class of AuthHandlerError."""
    assert issubclass(StaleCallbackError, AuthHandlerError)


def test_pcq_require_state_false_accepts_matching_state():
    """Permissive mode still accepts a matching state."""
    result = parse_callback_query(
        "code=ABC&state=S1", expected_state="S1", require_state=False
    )
    assert result == {"kind": "code", "code": "ABC", "state": "S1"}


# ---------------------------------------------------------------------
# CallbackServer construction errors
# ---------------------------------------------------------------------


def test_missing_certfile_for_https_raises_callback_server_error(tmp_path):
    """Constructing an https server without cert files must raise immediately."""
    port = _free_port()
    with pytest.raises(CallbackServerError, match="cert"):
        CallbackServer(
            f"https://127.0.0.1:{port}/cb",
            certfile=None,
            keyfile=None,
        )


def test_missing_keyfile_for_https_raises_callback_server_error(tmp_path):
    """certfile present but keyfile=None → CallbackServerError."""
    port = _free_port()
    dummy_cert = tmp_path / "cert.pem"
    dummy_cert.write_text("FAKE")
    with pytest.raises(CallbackServerError):
        CallbackServer(
            f"https://127.0.0.1:{port}/cb",
            certfile=dummy_cert,
            keyfile=None,
        )


def test_bind_failure_raises_callback_server_error():
    """Constructing a second server on the same port must raise CallbackServerError."""
    port = _free_port()
    first = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        with pytest.raises(CallbackServerError, match="port"):
            CallbackServer(f"http://127.0.0.1:{port}/cb")
    finally:
        first.close()


def test_callback_server_error_is_exception():
    assert issubclass(CallbackServerError, Exception)


# ---------------------------------------------------------------------
# CallbackServer — http happy path (test switch)
# ---------------------------------------------------------------------


def test_http_happy_path_code_capture():
    """GET ?code=abc&state=<expected> → wait() returns CodeResult.

    The server responds 200 with a success page body mentioning
    success/close-tab.
    """
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(server, expected_state="STATE-1", out=out)
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/cb?code=abc&state=STATE-1",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        assert resp.status_code == 200
        assert "exc" not in out, f"wait() raised: {out.get('exc')}"
        result = out["result"]
        assert result == {"kind": "code", "code": "abc", "state": "STATE-1"}
        # 200 body should hint the user to close the tab.
        body = resp.text.lower()
        assert "success" in body or "close" in body or "received" in body
    finally:
        server.close()


def test_http_happy_path_port_property():
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        assert server.port == port
    finally:
        server.close()


# ---------------------------------------------------------------------
# CallbackServer — https happy path (real tmp leaf cert)
# ---------------------------------------------------------------------


def test_https_happy_path_code_capture(tmp_path):
    """Full https round-trip with a real tmp leaf cert (from generate.py).

    The client uses ``verify=False`` because the CA is self-signed and
    not in the system trust store during tests.
    """
    certfile, keyfile = _tmp_leaf_cert_files(tmp_path)
    port = _free_port()
    server = CallbackServer(
        f"https://127.0.0.1:{port}/schwab/callback",
        certfile=certfile,
        keyfile=keyfile,
    )
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="STATE-HTTPS", out=out
    )
    try:
        resp = httpx.get(
            f"https://127.0.0.1:{port}/schwab/callback"
            f"?code=HTTPS-CODE&state=STATE-HTTPS",
            verify=False,
            follow_redirects=False,
            timeout=10,
        )
        t.join(timeout=10)
        assert resp.status_code == 200
        assert "exc" not in out, f"wait() raised: {out.get('exc')}"
        result = out["result"]
        assert result == {
            "kind": "code",
            "code": "HTTPS-CODE",
            "state": "STATE-HTTPS",
        }
    finally:
        server.close()


# ---------------------------------------------------------------------
# Security: missing state must NOT be accepted (CSRF hole closure)
# ---------------------------------------------------------------------


def test_missing_state_rejected_server_keeps_waiting():
    """GET ?code=abc (no state) must NOT be captured by the server.

    The server responds (possibly 200 with keep-waiting body or a
    non-2xx) but does NOT enqueue a result. A subsequent wait() with a
    short deadline therefore raises AuthHandlerError (timeout) because
    nothing valid arrived.

    This is the key CSRF regression test (plan §C1).
    """
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    cancel = threading.Event()
    # Use a tight deadline so the test doesn't hang long.
    t = _wait_result_in_thread(
        server,
        expected_state="STATE-2",
        deadline_offset=1.0,
        out=out,
    )
    try:
        # Issue the stateless GET — should NOT be accepted.
        httpx.get(
            f"http://127.0.0.1:{port}/cb?code=abc",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        # wait() must have timed out (nothing valid captured).
        assert "result" not in out, "Server accepted a no-state callback — CSRF hole!"
        assert "exc" in out
        assert isinstance(out["exc"], AuthHandlerError)
    finally:
        server.close()


# ---------------------------------------------------------------------
# State mismatch → benign continue; correct state captured after
# ---------------------------------------------------------------------


def test_state_mismatch_benign_continue_then_correct_captured():
    """A GET with wrong state must be ignored (not abort the wait).

    A subsequent GET with the correct state must be captured and returned.
    """
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="CORRECT-STATE", deadline_offset=10.0, out=out
    )
    try:
        # Wrong-state GET first — server keeps waiting.
        httpx.get(
            f"http://127.0.0.1:{port}/cb?code=BAD&state=WRONG-STATE",
            follow_redirects=False,
            timeout=5,
        )
        # Brief pause to ensure server processed the first request.
        time.sleep(0.1)
        # Correct-state GET — server captures.
        resp = httpx.get(
            f"http://127.0.0.1:{port}/cb?code=GOOD&state=CORRECT-STATE",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        assert resp.status_code == 200
        assert "exc" not in out, f"wait() raised: {out.get('exc')}"
        assert out["result"] == {
            "kind": "code",
            "code": "GOOD",
            "state": "CORRECT-STATE",
        }
    finally:
        server.close()


# ---------------------------------------------------------------------
# OAuth error param
# ---------------------------------------------------------------------


def test_oauth_error_param_captured():
    """GET ?error=access_denied&state=<expected> → wait() returns ErrorResult."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="STATE-ERR", deadline_offset=5.0, out=out
    )
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/cb?error=access_denied&state=STATE-ERR",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        assert resp.status_code == 200
        assert "exc" not in out, f"wait() raised: {out.get('exc')}"
        result = out["result"]
        assert result["kind"] == "error"
        assert result["error"] == "access_denied"
    finally:
        server.close()


def test_oauth_error_with_description_captured():
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="SE", deadline_offset=5.0, out=out
    )
    try:
        httpx.get(
            f"http://127.0.0.1:{port}/cb"
            f"?error=server_error&error_description=oops&state=SE",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        assert "exc" not in out
        assert out["result"]["error_description"] == "oops"
    finally:
        server.close()


# ---------------------------------------------------------------------
# Wrong path → 404 (not captured)
# ---------------------------------------------------------------------


def test_wrong_path_returns_404_and_not_captured():
    """A GET to any path other than the configured callback must return 404
    and must NOT capture anything."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server,
        expected_state="STATE-3",
        deadline_offset=1.0,
        out=out,
    )
    try:
        # Wrong path — not the configured /cb
        resp = httpx.get(
            f"http://127.0.0.1:{port}/wrong?code=X&state=STATE-3",
            follow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 404
        t.join(timeout=3)
        # wait() must have timed out (nothing captured on wrong path).
        assert "result" not in out
        assert "exc" in out
        assert isinstance(out["exc"], AuthHandlerError)
    finally:
        server.close()


def test_root_path_404_when_configured_callback_is_different():
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/schwab/callback")
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/",
            follow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 404
    finally:
        server.close()


# ---------------------------------------------------------------------
# Wrong method → 405
# ---------------------------------------------------------------------


def test_post_to_callback_path_returns_405():
    """POST is not a supported method on the callback endpoint."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/cb",
            content=b"code=abc&state=S",
            follow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 405
    finally:
        server.close()


def test_put_to_callback_path_returns_405():
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        resp = httpx.put(
            f"http://127.0.0.1:{port}/cb",
            content=b"",
            follow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 405
    finally:
        server.close()


# ---------------------------------------------------------------------
# Idempotent duplicate GET after successful capture
# ---------------------------------------------------------------------


def test_duplicate_get_after_capture_returns_200_already_received():
    """A second GET after the code was already captured must return 200
    with an 'already received' body and must NOT raise."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="STATE-IDEM", deadline_offset=5.0, out=out
    )
    try:
        # First GET — captures.
        resp1 = httpx.get(
            f"http://127.0.0.1:{port}/cb?code=ONCE&state=STATE-IDEM",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=5)
        assert resp1.status_code == 200
        assert "exc" not in out

        # Second GET — idempotent; server already captured.
        resp2 = httpx.get(
            f"http://127.0.0.1:{port}/cb?code=ONCE&state=STATE-IDEM",
            follow_redirects=False,
            timeout=5,
        )
        assert resp2.status_code == 200
        # Body should indicate the code was already received.
        body2 = resp2.text.lower()
        assert "already" in body2 or "received" in body2
    finally:
        server.close()


# ---------------------------------------------------------------------
# wait() deadline and cancel
# ---------------------------------------------------------------------


def test_wait_raises_authhandlererror_on_deadline():
    """If no valid GET arrives before the deadline, wait() raises AuthHandlerError."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        with pytest.raises(AuthHandlerError):
            server.wait(
                expected_state="STATE-TO",
                deadline=time.time() + 0.5,
                cancel=None,
            )
    finally:
        server.close()


def test_wait_raises_authhandlererror_on_cancel():
    """Setting the cancel event before wait() causes AuthHandlerError immediately."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(AuthHandlerError):
            server.wait(
                expected_state="STATE-CX",
                deadline=time.time() + 10,
                cancel=cancel,
            )
    finally:
        server.close()


def test_wait_cancel_mid_wait():
    """Cancel fired from another thread while wait() is blocking."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    cancel = threading.Event()
    out: dict = {}
    t = _wait_result_in_thread(
        server,
        expected_state="STATE-CX2",
        deadline_offset=30.0,
        cancel=cancel,
        out=out,
    )
    try:
        time.sleep(0.05)
        cancel.set()
        t.join(timeout=3)
        assert "exc" in out
        assert isinstance(out["exc"], AuthHandlerError)
    finally:
        server.close()


# ---------------------------------------------------------------------
# close() idempotent
# ---------------------------------------------------------------------


def test_close_is_idempotent():
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    server.close()
    server.close()  # must not raise


def test_close_releases_port():
    """After close(), the port should be available again (best-effort)."""
    port = _free_port()
    first = CallbackServer(f"http://127.0.0.1:{port}/cb")
    first.close()
    time.sleep(0.1)  # give OS a moment to release
    # Should not raise CallbackServerError now.
    second = CallbackServer(f"http://127.0.0.1:{port}/cb")
    second.close()


# ---------------------------------------------------------------------
# LocalServerHandler — AuthHandler Protocol wrapper
# ---------------------------------------------------------------------


def test_local_server_handler_wait_for_response_returns_result():
    """LocalServerHandler.wait_for_response delegates to the underlying
    CallbackServer and returns the captured AuthResult."""
    port = _free_port()
    handler = LocalServerHandler(f"http://127.0.0.1:{port}/cb")
    out: dict = {}

    def _send():
        # Give the handler a moment to bind and start serving.
        time.sleep(0.05)
        httpx.get(
            f"http://127.0.0.1:{port}/cb?code=LS-CODE&state=LS-STATE",
            follow_redirects=False,
            timeout=5,
        )

    t_send = threading.Thread(target=_send, daemon=True)
    t_send.start()
    try:
        result = handler.wait_for_response(expected_state="LS-STATE")
        assert result == {"kind": "code", "code": "LS-CODE", "state": "LS-STATE"}
    finally:
        t_send.join(timeout=5)
        # Clean up underlying server if handler exposes it.
        if hasattr(handler, "_server") or hasattr(handler, "server"):
            srv = getattr(handler, "_server", None) or getattr(handler, "server", None)
            if srv is not None:
                srv.close()


def test_local_server_handler_respects_cancel():
    """LocalServerHandler.wait_for_response raises AuthHandlerError when
    cancel is set before the first valid GET."""
    port = _free_port()
    handler = LocalServerHandler(f"http://127.0.0.1:{port}/cb")
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(AuthHandlerError):
            handler.wait_for_response(expected_state="S", cancel=cancel)
    finally:
        if hasattr(handler, "_server") or hasattr(handler, "server"):
            srv = getattr(handler, "_server", None) or getattr(handler, "server", None)
            if srv is not None:
                srv.close()


def test_local_server_handler_satisfies_auth_handler_protocol():
    """LocalServerHandler must satisfy the AuthHandler Protocol structurally."""
    from schwab_cli.auth_handlers import AuthHandler

    # Type-level check via isinstance with a runtime-checkable Protocol,
    # or simply confirm the method exists with the right signature.
    port = _free_port()
    handler = LocalServerHandler(f"http://127.0.0.1:{port}/cb")
    try:
        assert hasattr(handler, "wait_for_response")
        assert callable(handler.wait_for_response)
    finally:
        if hasattr(handler, "_server") or hasattr(handler, "server"):
            srv = getattr(handler, "_server", None) or getattr(handler, "server", None)
            if srv is not None:
                srv.close()


# ---------------------------------------------------------------------
# No code, no error param → 400 (keep waiting)
# ---------------------------------------------------------------------


def test_no_code_no_error_param_returns_400_and_not_captured():
    """A GET with state but neither code nor error must return 400 and
    not capture anything (server keeps waiting)."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server,
        expected_state="STATE-4",
        deadline_offset=1.0,
        out=out,
    )
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/cb?state=STATE-4&foo=bar",
            follow_redirects=False,
            timeout=5,
        )
        # Server should reject with 4xx (bad request — no code or error).
        assert resp.status_code in (400, 422)
        t.join(timeout=3)
        # Nothing valid was captured.
        assert "result" not in out
        assert "exc" in out
        assert isinstance(out["exc"], AuthHandlerError)
    finally:
        server.close()


def test_local_server_handler_closes_server_after_success():
    """After a successful capture the handler must tear down the underlying
    server (stop the serve thread) — otherwise the thread + bound port leak
    and the next auth attempt fails with 'port in use'."""
    port = _free_port()
    handler = LocalServerHandler(f"http://127.0.0.1:{port}/cb")

    def _send():
        time.sleep(0.05)
        httpx.get(
            f"http://127.0.0.1:{port}/cb?code=C&state=S",
            follow_redirects=False,
            timeout=5,
        )

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    result = handler.wait_for_response(expected_state="S")
    t.join(timeout=5)
    assert result == {"kind": "code", "code": "C", "state": "S"}

    # The serve thread must have stopped (shutdown runs on a side thread, so
    # allow a brief moment) — proving the handler released the server.
    deadline = time.time() + 3.0
    while handler._server._thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert not handler._server._thread.is_alive()
    assert handler._server._closed is True


def test_error_param_without_state_is_captured_at_server_level():
    """An OAuth error response with no state echoed is still captured as an
    ErrorResult (RFC 6749 permits missing state on error) — it must not hang."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="ES", deadline_offset=2.0, out=out
    )
    try:
        httpx.get(
            f"http://127.0.0.1:{port}/cb?error=access_denied",
            follow_redirects=False,
            timeout=5,
        )
        t.join(timeout=3)
        assert out.get("result", {}).get("kind") == "error"
        assert out["result"]["error"] == "access_denied"
    finally:
        server.close()


def test_get_before_wait_is_not_captured():
    """A GET that lands before wait() sets expected_state is treated as
    keep-waiting (benign 200) and not captured."""
    port = _free_port()
    server = CallbackServer(f"http://127.0.0.1:{port}/cb")
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/cb?code=EARLY&state=S",
            follow_redirects=False,
            timeout=5,
        )
        assert resp.status_code == 200
        assert server._captured is False
    finally:
        server.close()


def test_http_to_non_loopback_host_is_refused():
    """Plaintext http to a non-loopback host must be refused, so an OAuth
    code is never served in the clear."""
    with pytest.raises(CallbackServerError):
        CallbackServer("http://example.com:8080/cb")


# ---------------------------------------------------------------------
# Timeout diagnostics — the message must self-explain (no secrets)
# ---------------------------------------------------------------------


def test_timeout_message_reports_zero_requests_when_browser_never_arrives():
    """The headline daemon signal: requests=0 → the redirect never reached us."""
    port = _free_port()
    server = CallbackServer(_http_uri(port))
    try:
        with pytest.raises(AuthHandlerError) as exc:
            server.wait(
                expected_state="S",
                deadline=time.time() + 0.3,
                cancel=None,
            )
        msg = str(exc.value)
        assert "timed out" in msg
        assert "requests=0" in msg
        assert f":{port}/cb" in msg  # bind target surfaced
    finally:
        server.close()


def test_timeout_message_counts_stale_state_get():
    """A GET with the wrong state is tallied as stale_state, not a no-show."""
    port = _free_port()
    server = CallbackServer(_http_uri(port))
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="RIGHT", deadline_offset=1.5, out=out
    )
    try:
        httpx.get(
            f"http://127.0.0.1:{port}/cb?code=C&state=WRONG",
            follow_redirects=False, timeout=5,
        )
        t.join(timeout=4)
        assert "exc" in out and isinstance(out["exc"], AuthHandlerError)
        msg = str(out["exc"])
        assert "requests=1" in msg
        assert "stale_state=1" in msg
    finally:
        server.close()


def test_timeout_message_counts_wrong_path_get():
    """A GET to the wrong path is tallied as wrong_path."""
    port = _free_port()
    server = CallbackServer(_http_uri(port))
    out: dict = {}
    t = _wait_result_in_thread(
        server, expected_state="S", deadline_offset=1.5, out=out
    )
    try:
        httpx.get(
            f"http://127.0.0.1:{port}/nope?code=C&state=S",
            follow_redirects=False, timeout=5,
        )
        t.join(timeout=4)
        assert "exc" in out
        msg = str(out["exc"])
        assert "wrong_path=1" in msg
    finally:
        server.close()
