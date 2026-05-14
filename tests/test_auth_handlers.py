"""Tests for `schwab_cli.auth_handlers`.

Two concrete handlers ship today:
  * ``UserInputHandler`` — prompts on stderr, reads from stdin.
  * ``CodeRelayHandler`` — long-polls a configured relay URL.

Both return ``AuthResult`` (``CodeResult`` today). ``TokenResult`` is the
future shape for ``AuthServerHandler`` and is type-checked here against
the ``AuthHandler`` Protocol.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from schwab_cli.auth_handlers import (
    AuthHandler,
    AuthHandlerError,
    AuthResult,
    CodeRelayHandler,
    ErrorResult,
    UserInputHandler,
)


# ---------------------------------------------------------------------
# UserInputHandler
# ---------------------------------------------------------------------


def _set_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_user_input_parses_bare_code(monkeypatch, capsys):
    _set_stdin(monkeypatch, "C0.bare-code-value\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-ABC")
    assert r == {"kind": "code", "code": "C0.bare-code-value", "state": None}
    err = capsys.readouterr().err
    assert "state verification skipped" in err.lower()


def test_user_input_parses_querystring(monkeypatch):
    _set_stdin(monkeypatch, "code=C0.x.y.z&state=STATE-ABC&session=foo\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-ABC")
    assert r["kind"] == "code"
    assert r["code"] == "C0.x.y.z"
    assert r["state"] == "STATE-ABC"


def test_user_input_parses_full_url(monkeypatch):
    _set_stdin(
        monkeypatch,
        "https://127.0.0.1:8443/?code=C0.url-form&state=STATE-XYZ&session=x\n",
    )
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-XYZ")
    assert r["code"] == "C0.url-form"
    assert r["state"] == "STATE-XYZ"


def test_user_input_rejects_state_mismatch(monkeypatch):
    _set_stdin(monkeypatch, "code=ABC&state=WRONG\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError, match="state"):
        h.wait_for_response(expected_state="EXPECTED")


def test_user_input_rejects_empty(monkeypatch):
    _set_stdin(monkeypatch, "\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError):
        h.wait_for_response(expected_state="S")


def test_user_input_parses_error_querystring(monkeypatch):
    """``?error=access_denied&error_description=...&state=S`` → ErrorResult."""
    _set_stdin(
        monkeypatch,
        "error=access_denied&error_description=user+rejected&state=STATE-S\n",
    )
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-S")
    assert r == {
        "kind": "error",
        "error": "access_denied",
        "error_description": "user rejected",
        "state": "STATE-S",
    }


def test_user_input_parses_error_full_url(monkeypatch):
    _set_stdin(
        monkeypatch,
        "https://127.0.0.1:8443/?error=server_error&"
        "error_description=oops&state=STATE-X\n",
    )
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-X")
    assert r["kind"] == "error"
    assert r["error"] == "server_error"
    assert r["error_description"] == "oops"
    assert r["state"] == "STATE-X"


def test_user_input_error_without_description(monkeypatch):
    _set_stdin(monkeypatch, "error=invalid_request&state=S\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "error"
    assert r["error"] == "invalid_request"
    assert r["error_description"] is None


def test_user_input_error_without_state_is_permissive(monkeypatch):
    """RFC 6749 says providers SHOULD echo state in error responses, but
    some skip it. Accept the error rather than refuse."""
    _set_stdin(monkeypatch, "error=server_error\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "error"
    assert r["error"] == "server_error"
    assert r["state"] is None


def test_user_input_rejects_state_mismatch_on_error(monkeypatch):
    """State mismatch on an error response is still suspicious — reject."""
    _set_stdin(monkeypatch, "error=access_denied&state=WRONG\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError, match="state mismatch on error"):
        h.wait_for_response(expected_state="EXPECTED")


def test_user_input_rejects_querystring_without_code(monkeypatch):
    _set_stdin(monkeypatch, "state=S&foo=bar\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError, match="code"):
        h.wait_for_response(expected_state="S")


def test_user_input_returns_code_kind_discriminator(monkeypatch):
    """Sanity-check the AuthResult shape carries the kind discriminator."""
    _set_stdin(monkeypatch, "code=A&state=S\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "code"


# ---------------------------------------------------------------------
# CodeRelayHandler
# ---------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_httpx_get(monkeypatch, side_effect):
    """side_effect: callable taking (url, **kwargs) returning _FakeResponse,
    or raising an exception."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        result = side_effect(url, **kwargs) if callable(side_effect) else side_effect
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("httpx.get", fake_get)
    return calls


def test_relay_returns_code_on_200(monkeypatch):
    _patch_httpx_get(
        monkeypatch,
        lambda url, **kw: _FakeResponse(200, "code=ABC&state=S"),
    )
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r == {"kind": "code", "code": "ABC", "state": "S"}


def test_relay_returns_error_result_on_oauth_error(monkeypatch):
    """Relay returning a querystring with ``error=`` produces ErrorResult,
    not AuthHandlerError. The race short-circuits authoritatively."""
    _patch_httpx_get(
        monkeypatch,
        lambda url, **kw: _FakeResponse(
            200, "error=access_denied&error_description=user+rejected&state=S",
        ),
    )
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r == {
        "kind": "error",
        "error": "access_denied",
        "error_description": "user rejected",
        "state": "S",
    }


def test_relay_retries_on_408(monkeypatch):
    seq = [_FakeResponse(408), _FakeResponse(408), _FakeResponse(200, "code=Z&state=S")]

    def side(url, **kw):
        return seq.pop(0)

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r["code"] == "Z"


def test_relay_retries_on_read_timeout(monkeypatch):
    calls = [0]

    def side(url, **kw):
        calls[0] += 1
        if calls[0] == 1:
            return httpx.ReadTimeout("slow")
        return _FakeResponse(200, "code=T&state=S")

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r["code"] == "T"
    assert calls[0] == 2


def test_relay_raises_on_403(monkeypatch):
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(403, "denied"))
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="403"):
        h.wait_for_response(expected_state="S")


def test_relay_raises_on_unexpected_status(monkeypatch):
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(500, "boom"))
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="500"):
        h.wait_for_response(expected_state="S")


def test_relay_raises_on_state_mismatch(monkeypatch):
    _patch_httpx_get(
        monkeypatch,
        lambda url, **kw: _FakeResponse(200, "code=X&state=WRONG"),
    )
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="state"):
        h.wait_for_response(expected_state="EXPECTED")


def test_relay_cancels_between_polls(monkeypatch):
    """Setting the cancel event between polls should make the handler raise
    AuthHandlerError so the race aggregator can record it as a non-winner.
    """
    cancel = threading.Event()
    calls = [0]

    def side(url, **kw):
        calls[0] += 1
        # First call: pretend the relay had nothing yet (408). Set cancel.
        cancel.set()
        return _FakeResponse(408)

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="cancel"):
        h.wait_for_response(expected_state="S", cancel=cancel)
    assert calls[0] == 1  # didn't loop after cancel


def test_relay_raises_after_deadline(monkeypatch):
    """If the relay keeps 408'ing past the deadline, raise."""
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(408))
    h = CodeRelayHandler("https://relay/wait", deadline_seconds=0.1)
    start = time.time()
    with pytest.raises(AuthHandlerError):
        h.wait_for_response(expected_state="S")
    assert time.time() - start < 5  # bounded


# ---------------------------------------------------------------------
# Protocol type seam
# ---------------------------------------------------------------------


def test_token_result_handler_satisfies_protocol():
    """Future AuthServerHandler will return TokenResult. The Protocol
    must accept that shape via duck typing."""

    class _FakeTokenHandler:
        def wait_for_response(
            self, *, expected_state: str, cancel=None,
        ) -> AuthResult:
            return {
                "kind": "token",
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 1800,
            }

    h: AuthHandler = _FakeTokenHandler()  # type-only assertion via assignment
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "token"


# ---------------------------------------------------------------------
# HttpNotificationListener
# ---------------------------------------------------------------------

import json
import re
import socket
import threading as _threading
import urllib.request
import urllib.error

from schwab_cli.auth_handlers import HttpNotificationListener


def _post_json(endpoint, payload, *, method="POST", extra_path=""):
    """Helper: POST a JSON body to ``endpoint + extra_path``. Returns
    ``(status, body_bytes)`` or raises on transport failure."""
    url = endpoint + extra_path
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(
        url, data=body if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_listener_endpoint_matches_expected_shape():
    listener = HttpNotificationListener()
    try:
        assert re.match(
            r"^http://127\.0\.0\.1:\d+/oauth/[0-9a-f]{8}$", listener.endpoint,
        ), listener.endpoint
        assert listener.transport_type == "http"
    finally:
        listener.close()


def test_listener_token_entropy_changes_per_instance():
    a = HttpNotificationListener()
    b = HttpNotificationListener()
    try:
        assert a.endpoint != b.endpoint
    finally:
        a.close()
        b.close()


def test_listener_accepts_code_post():
    listener = HttpNotificationListener()
    try:
        # POST from a separate thread (the listener's wait() blocks).
        result_box = {}
        def _send():
            status, _ = _post_json(
                listener.endpoint,
                {"kind": "code", "code": "C0.x", "state": "S"},
            )
            result_box["status"] = status
        t = _threading.Thread(target=_send, daemon=True)
        t.start()
        result = listener.wait(
            expected_state="S", deadline=time.time() + 3, cancel=None,
        )
        t.join(timeout=2)
        assert result == {"kind": "code", "code": "C0.x", "state": "S"}
        assert result_box["status"] == 200
    finally:
        listener.close()


def test_listener_accepts_error_post():
    listener = HttpNotificationListener()
    try:
        def _send():
            _post_json(
                listener.endpoint,
                {"kind": "error", "error": "access_denied",
                 "error_description": "user rejected", "state": "S"},
            )
        _threading.Thread(target=_send, daemon=True).start()
        result = listener.wait(
            expected_state="S", deadline=time.time() + 3, cancel=None,
        )
        assert result == {
            "kind": "error", "error": "access_denied",
            "error_description": "user rejected", "state": "S",
        }
    finally:
        listener.close()


def test_listener_accepts_token_post_without_state():
    """kind='token' doesn't need state echo — the upstream did the
    OAuth exchange, no externally visible state to confuse with another flow."""
    listener = HttpNotificationListener()
    try:
        def _send():
            _post_json(
                listener.endpoint,
                {"kind": "token", "access_token": "A",
                 "refresh_token": "R", "expires_in": 1800},
            )
        _threading.Thread(target=_send, daemon=True).start()
        result = listener.wait(
            expected_state="S", deadline=time.time() + 3, cancel=None,
        )
        assert result == {
            "kind": "token", "access_token": "A",
            "refresh_token": "R", "expires_in": 1800,
        }
    finally:
        listener.close()


def test_listener_rejects_state_mismatch_with_authhandlererror():
    listener = HttpNotificationListener()
    try:
        def _send():
            _post_json(
                listener.endpoint,
                {"kind": "code", "code": "X", "state": "WRONG"},
            )
        _threading.Thread(target=_send, daemon=True).start()
        with pytest.raises(AuthHandlerError, match="state mismatch"):
            listener.wait(
                expected_state="EXPECTED",
                deadline=time.time() + 3, cancel=None,
            )
    finally:
        listener.close()


def test_listener_404_on_wrong_path():
    listener = HttpNotificationListener()
    try:
        base_url = listener.endpoint.rsplit("/oauth/", 1)[0]
        # No path segment.
        status, _ = _post_json(
            base_url + "/oauth", {"kind": "code", "code": "X", "state": "S"},
        )
        assert status == 404
        # Wrong token.
        status, _ = _post_json(
            base_url + "/oauth/deadbeef",
            {"kind": "code", "code": "X", "state": "S"},
        )
        assert status == 404
        # Root.
        status, _ = _post_json(
            base_url + "/", {"kind": "code", "code": "X", "state": "S"},
        )
        assert status == 404
    finally:
        listener.close()


def test_listener_405_on_wrong_method():
    listener = HttpNotificationListener()
    try:
        status, _ = _post_json(listener.endpoint, None, method="GET")
        assert status == 405
    finally:
        listener.close()


def test_listener_400_on_malformed_json_keeps_listening():
    """Bad shape doesn't stop the listener — a later legit POST can win."""
    listener = HttpNotificationListener()
    try:
        # Malformed first.
        req = urllib.request.Request(
            listener.endpoint, data=b"not json {{{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # Then a valid POST — listener should still respond.
        def _send():
            _post_json(
                listener.endpoint,
                {"kind": "code", "code": "RECOVERED", "state": "S"},
            )
        _threading.Thread(target=_send, daemon=True).start()
        result = listener.wait(
            expected_state="S", deadline=time.time() + 3, cancel=None,
        )
        assert result["code"] == "RECOVERED"
    finally:
        listener.close()


def test_listener_timeout_raises():
    listener = HttpNotificationListener()
    try:
        with pytest.raises(AuthHandlerError, match="timed out"):
            listener.wait(
                expected_state="S",
                deadline=time.time() + 0.3,
                cancel=None,
            )
    finally:
        listener.close()


def test_listener_cancel_event_raises():
    listener = HttpNotificationListener()
    try:
        cancel = _threading.Event()
        cancel.set()
        with pytest.raises(AuthHandlerError, match="cancelled"):
            listener.wait(
                expected_state="S", deadline=time.time() + 3, cancel=cancel,
            )
    finally:
        listener.close()


def test_listener_close_is_idempotent():
    listener = HttpNotificationListener()
    listener.close()
    listener.close()  # no exception
    # Subsequent connect attempt should fail (best-effort — give the OS
    # a moment to release the port).
    time.sleep(0.1)
    with pytest.raises(Exception):
        _post_json(listener.endpoint, {"kind": "code", "code": "X", "state": "S"})


# ---------------------------------------------------------------------
# AutoLoginHandler  (fixture-script driven)
# ---------------------------------------------------------------------

from schwab_cli.auth_handlers import AutoLoginHandler


def _write_fixture_script(tmp_path, body: str):
    """Write a Python script that stands in for ``webauto-cli`` — it
    parses the flags schwab_cli passes and acts on them."""
    script = tmp_path / "fake_webauto.py"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)
    return script


_FIXTURE_PARSE_AND_POST = r"""
import argparse, json, sys, urllib.request, os

# Side-effect for argv-assembly tests — write BEFORE we do anything else
# so SIGTERM after the POST doesn't beat us to it.
if os.environ.get("FIXTURE_ARGV_SIDECAR"):
    open(os.environ["FIXTURE_ARGV_SIDECAR"], "w").write(json.dumps(sys.argv))

ap = argparse.ArgumentParser()
ap.add_argument("--notification-endpoint")
ap.add_argument("--state")
ap.add_argument("-a", action="append", default=[])
args, _ = ap.parse_known_args()

mode = os.environ.get("FIXTURE_MODE", "code")
if mode == "code":
    payload = {"kind": "code", "code": "FROM-FIXTURE", "state": args.state}
elif mode == "error":
    payload = {"kind": "error", "error": "access_denied",
               "error_description": "rejected", "state": args.state}
elif mode == "wrong_state":
    payload = {"kind": "code", "code": "X", "state": "WRONG"}
elif mode == "exit_no_post":
    sys.exit(int(os.environ.get("FIXTURE_EXIT", "1")))
elif mode == "sleep":
    import time as _t; _t.sleep(99)
else:
    raise SystemExit("unknown FIXTURE_MODE")

req = urllib.request.Request(
    args.notification_endpoint,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=3).read()
"""


def test_auto_login_handler_happy_path_code(tmp_path, monkeypatch):
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "code")
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth?state=S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    result = handler.wait_for_response(expected_state="S")
    assert result == {"kind": "code", "code": "FROM-FIXTURE", "state": "S"}


def test_auto_login_handler_oauth_error_path(tmp_path, monkeypatch):
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "error")
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth?state=S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    result = handler.wait_for_response(expected_state="S")
    assert result["kind"] == "error"
    assert result["error"] == "access_denied"


def test_auto_login_handler_state_mismatch_raises(tmp_path, monkeypatch):
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "wrong_state")
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    with pytest.raises(AuthHandlerError, match="state mismatch"):
        handler.wait_for_response(expected_state="EXPECTED")


def test_auto_login_handler_exit_without_post(tmp_path, monkeypatch):
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "exit_no_post")
    monkeypatch.setenv("FIXTURE_EXIT", "7")
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    with pytest.raises(AuthHandlerError, match="rc=7"):
        handler.wait_for_response(expected_state="S")


def test_auto_login_handler_timeout(tmp_path, monkeypatch):
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "sleep")
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=0.5,
    )
    with pytest.raises(AuthHandlerError, match="timed out"):
        handler.wait_for_response(expected_state="S")


def test_auto_login_handler_argv_assembly(tmp_path, monkeypatch):
    """Asserts the three appended flags are in argv in the right order."""
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    sidecar = tmp_path / "argv.json"
    monkeypatch.setenv("FIXTURE_MODE", "code")
    monkeypatch.setenv("FIXTURE_ARGV_SIDECAR", str(sidecar))
    handler = AutoLoginHandler(
        [sys.executable, str(fixture), "--baseflag", "from-config"],
        auth_url="https://schwab/auth?id=X",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    handler.wait_for_response(expected_state="S")
    # Give the fixture a moment to write its sidecar.
    for _ in range(20):
        if sidecar.exists():
            break
        time.sleep(0.05)
    argv = json.loads(sidecar.read_text())
    assert "--baseflag" in argv
    assert "from-config" in argv
    assert "--notification-endpoint" in argv
    assert "--state" in argv
    assert argv[argv.index("--state") + 1] == "S"
    # --log flag presence is governed by DEBUG env (covered in its own test).
    # -a URL=... passed
    assert "-a" in argv
    a_indices = [i for i, t in enumerate(argv) if t == "-a"]
    a_values = [argv[i + 1] for i in a_indices]
    assert any(v == "URL=https://schwab/auth?id=X" for v in a_values)


def test_auto_login_handler_adds_log_flag_when_debug_env_set(
    tmp_path, monkeypatch,
):
    """With DEBUG=1 in env, schwab_cli adds ``--log <path>`` so webauto
    writes a structured log."""
    sidecar = tmp_path / "argv.json"
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "code")
    monkeypatch.setenv("FIXTURE_ARGV_SIDECAR", str(sidecar))
    monkeypatch.setenv("DEBUG", "1")
    log_dir = tmp_path / "logs"
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        stderr_log_dir=log_dir,
        timeout_seconds=10.0,
    )
    handler.wait_for_response(expected_state="S")
    for _ in range(20):
        if sidecar.exists():
            break
        time.sleep(0.05)
    argv = json.loads(sidecar.read_text())
    assert "--log" in argv
    log_path = Path(argv[argv.index("--log") + 1])
    assert log_path.parent == log_dir
    assert log_path.name.startswith("auto_login-")
    assert log_path.name.endswith(".stderr.log")
    assert log_dir.exists()


def test_auto_login_handler_skips_log_flag_without_debug(
    tmp_path, monkeypatch,
):
    """Without DEBUG, schwab_cli does NOT add ``--log``. webauto runs
    quiet (its stderr goes to subprocess.DEVNULL on schwab_cli's side)."""
    sidecar = tmp_path / "argv.json"
    fixture = _write_fixture_script(tmp_path, _FIXTURE_PARSE_AND_POST)
    monkeypatch.setenv("FIXTURE_MODE", "code")
    monkeypatch.setenv("FIXTURE_ARGV_SIDECAR", str(sidecar))
    monkeypatch.delenv("DEBUG", raising=False)
    log_dir = tmp_path / "logs"
    handler = AutoLoginHandler(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        stderr_log_dir=log_dir,
        timeout_seconds=10.0,
    )
    handler.wait_for_response(expected_state="S")
    for _ in range(20):
        if sidecar.exists():
            break
        time.sleep(0.05)
    argv = json.loads(sidecar.read_text())
    assert "--log" not in argv
    # Without --log we don't pre-create the dir either.
    assert not log_dir.exists()


# ---------------------------------------------------------------------
# AutoLoginSupervisor
# ---------------------------------------------------------------------

from schwab_cli.auth_handlers import AutoLoginSupervisor


def test_supervisor_start_spawns_subprocess_with_log_dir_when_debug(
    tmp_path, monkeypatch,
):
    """With DEBUG=1, the log dir is pre-created so webauto can write its
    ``--log`` file there. Without DEBUG, ``--log`` isn't added and the
    dir isn't touched."""
    fixture = _write_fixture_script(
        tmp_path, 'import time; time.sleep(0.2)\n',
    )
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("DEBUG", "1")
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=log_dir,
        timeout_seconds=10.0,
    )
    sup.start()
    assert sup._proc is not None
    assert log_dir.exists()
    sup.terminate()


def test_supervisor_start_skips_log_dir_without_debug(tmp_path, monkeypatch):
    fixture = _write_fixture_script(
        tmp_path, 'import time; time.sleep(0.2)\n',
    )
    log_dir = tmp_path / "logs"
    monkeypatch.delenv("DEBUG", raising=False)
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=log_dir,
        timeout_seconds=10.0,
    )
    sup.start()
    assert sup._proc is not None
    assert not log_dir.exists()
    sup.terminate()


def test_supervisor_argv_includes_no_notify(tmp_path):
    sidecar = tmp_path / "sargv.json"
    fixture = _write_fixture_script(
        tmp_path,
        f'import sys, json, os; '
        f'open({str(sidecar)!r}, "w").write(json.dumps(sys.argv)); '
        f'import time; time.sleep(0.2)\n',
    )
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth?x=1",
        state="MY_STATE",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    sup.start()
    # Wait for fixture to write the sidecar.
    for _ in range(30):
        if sidecar.exists():
            break
        time.sleep(0.05)
    sup.terminate()
    argv = json.loads(sidecar.read_text())
    assert "--no-notify" in argv
    assert "--state" in argv
    assert argv[argv.index("--state") + 1] == "MY_STATE"
    # --log flag presence is governed by DEBUG env (covered separately).
    # URL passthrough
    assert "-a" in argv
    a_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-a"]
    assert any(v == "URL=https://schwab/auth?x=1" for v in a_values)


def test_supervisor_terminate_is_idempotent(tmp_path):
    fixture = _write_fixture_script(tmp_path, 'import time; time.sleep(0.1)\n')
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    sup.start()
    sup.terminate()
    sup.terminate()  # no exception


def test_supervisor_terminate_kills_long_running(tmp_path):
    """SIGTERM → 5s → SIGKILL — but with a short-running test we just
    confirm terminate() returns within the grace window when the
    subprocess does exit on SIGTERM."""
    fixture = _write_fixture_script(tmp_path, 'import time; time.sleep(60)\n')
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=60.0,
    )
    sup.start()
    start = time.time()
    sup.terminate()
    elapsed = time.time() - start
    # Should complete well within 5s grace + SIGKILL.
    assert elapsed < 6.5, f"terminate() took {elapsed}s"
