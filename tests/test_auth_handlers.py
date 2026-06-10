"""Tests for :mod:`schwab_cli.auth_handlers`.

Post-refactor contract:
  - CodeRelayHandler IS REMOVED (raises AttributeError/ImportError)
  - HttpNotificationListener IS REMOVED
  - AutoLoginHandler IS REMOVED
  - _validate_payload IS REMOVED

  KEPT:
  - UserInputHandler (paste fallback, always present on human path)
  - _from_querystring (querystring parsing helper)
  - _parse_user_input (sniff input shape)
  - AutoLoginSupervisor (side-effect for local_server with auto_login_command)
  - _build_webauto_argv (argv builder)
  - _terminate (subprocess teardown)
  - AuthHandler, AuthResult, CodeResult, TokenResult, ErrorResult (types)
  - AuthHandlerError, StaleCallbackError (exceptions)
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from schwab_cli.auth_handlers import (
    AuthHandler,
    AuthHandlerError,
    AuthResult,
    StaleCallbackError,
    UserInputHandler,
)


# ---- Proof that removed names are gone -------------------------------------


def test_code_relay_handler_is_removed():
    """CodeRelayHandler must no longer be importable from auth_handlers."""
    import schwab_cli.auth_handlers as ah
    assert not hasattr(ah, "CodeRelayHandler"), (
        "CodeRelayHandler must be removed from auth_handlers"
    )


def test_http_notification_listener_is_removed():
    """HttpNotificationListener must no longer be importable from auth_handlers."""
    import schwab_cli.auth_handlers as ah
    assert not hasattr(ah, "HttpNotificationListener"), (
        "HttpNotificationListener must be removed from auth_handlers"
    )


def test_auto_login_handler_is_removed():
    """AutoLoginHandler must no longer be importable from auth_handlers."""
    import schwab_cli.auth_handlers as ah
    assert not hasattr(ah, "AutoLoginHandler"), (
        "AutoLoginHandler must be removed from auth_handlers"
    )


def test_validate_payload_is_removed():
    """_validate_payload must no longer be importable from auth_handlers."""
    import schwab_cli.auth_handlers as ah
    assert not hasattr(ah, "_validate_payload"), (
        "_validate_payload must be removed from auth_handlers"
    )


# ---- UserInputHandler ------------------------------------------------------


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


# ---- _from_querystring (parse helper — kept) --------------------------------


from schwab_cli.auth_handlers import _from_querystring


def test_from_querystring_parses_code_with_state():
    r = _from_querystring("code=MY_CODE&state=MY_STATE", expected_state="MY_STATE")
    assert r == {"kind": "code", "code": "MY_CODE", "state": "MY_STATE"}


def test_from_querystring_raises_on_state_mismatch():
    with pytest.raises(StaleCallbackError):
        _from_querystring("code=X&state=WRONG", expected_state="EXPECTED")


def test_from_querystring_error_path():
    r = _from_querystring(
        "error=access_denied&error_description=nope&state=S",
        expected_state="S",
    )
    assert r["kind"] == "error"
    assert r["error"] == "access_denied"


def test_from_querystring_missing_code_raises():
    with pytest.raises(AuthHandlerError, match="code"):
        _from_querystring("state=S&foo=bar", expected_state="S")


def test_stale_callback_error_is_authhandlererror():
    """Paste path relies on StaleCallbackError still being an
    AuthHandlerError so a pasted stale URL is surfaced to the user
    (not silently swallowed). Subclass relationship is the contract."""
    assert issubclass(StaleCallbackError, AuthHandlerError)


# ---- Protocol type seam ----------------------------------------------------


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


# ---- AutoLoginSupervisor (KEPT) --------------------------------------------


from schwab_cli.auth_handlers import AutoLoginSupervisor


def _write_fixture_script(tmp_path, body: str):
    """Write a Python script that stands in for ``webauto-cli``."""
    script = tmp_path / "fake_webauto.py"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)
    return script


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


def test_supervisor_creates_log_dir_even_without_debug(tmp_path, monkeypatch):
    # The trace is now always-on, so the log dir is created (and a --log
    # file written) even when the parent has no DEBUG — that's the whole
    # point: the daemon's unattended runs leave a trace.
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
    # The dir is created and a --log path is passed; the file itself is
    # written by the real webauto-cli (the fake fixture here ignores --log).
    assert log_dir.exists()
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
    assert "-a" in argv
    a_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-a"]
    assert any(v == "URL=https://schwab/auth?x=1" for v in a_values)


def test_supervisor_forces_debug_in_child_env(tmp_path, monkeypatch):
    # Parent has no DEBUG; the supervisor must still spawn the child with
    # DEBUG=1 so the always-on --log trace is verbose.
    monkeypatch.delenv("DEBUG", raising=False)
    sidecar = tmp_path / "debug.txt"
    fixture = _write_fixture_script(
        tmp_path,
        f'import os, time; '
        f'open({str(sidecar)!r}, "w").write(os.environ.get("DEBUG", "")); '
        f'time.sleep(0.2)\n',
    )
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    sup.start()
    for _ in range(30):
        if sidecar.exists():
            break
        time.sleep(0.05)
    sup.terminate()
    assert sidecar.read_text() == "1"


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
    assert elapsed < 6.5, f"terminate() took {elapsed}s"


def test_supervisor_terminate_is_thread_safe_under_concurrency(tmp_path):
    """terminate() may be called by the watchdog Timer thread and the
    caller's finally at the same time. Hammering it from many threads
    while the watchdog is armed must never raise (regression for the
    ``self._watchdog.cancel()`` / ``self._proc`` TOCTOU)."""
    fixture = _write_fixture_script(tmp_path, "import time; time.sleep(30)\n")
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=60.0,  # watchdog armed but won't fire during the test
    )
    sup.start()
    pid = sup._proc.pid
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def hammer() -> None:
        try:
            barrier.wait()
            sup.terminate()
        except BaseException as exc:  # noqa: BLE001 — capture any race fault
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"terminate() raced: {errors!r}"
    # Subprocess is gone and the watchdog was cancelled.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert sup._proc is None and sup._watchdog is None


def test_supervisor_start_leads_its_own_process_group(tmp_path):
    """The subprocess must lead its own session/process group so the whole
    tree (webauto-cli → seleniumbase → uc_driver → chrome) can be signalled
    together. ``getpgid(pid) == pid`` proves it is the group leader, i.e.
    ``start_new_session=True`` was used."""
    fixture = _write_fixture_script(tmp_path, "import time; time.sleep(5)\n")
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=10.0,
    )
    sup.start()
    try:
        pid = sup._proc.pid
        assert os.getpgid(pid) == pid, (
            "subprocess is not its own process-group leader; "
            "start_new_session=True is required so killpg reaches the tree"
        )
    finally:
        sup.terminate()


def test_supervisor_terminate_reaps_grandchild_tree(tmp_path):
    """Regression for the uc_driver leak: ``terminate()`` must kill the
    subprocess's *descendants* (the browser-driver tree), not just the
    direct child. The fixture spawns a long-lived grandchild (stand-in for
    uc_driver/chrome) and records its PID; after ``terminate()`` that
    grandchild must be gone."""
    sidecar = tmp_path / "grandchild.pid"
    fixture = _write_fixture_script(
        tmp_path,
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(sidecar)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(120)\n",
    )
    sup = AutoLoginSupervisor(
        [sys.executable, str(fixture)],
        auth_url="https://schwab/auth",
        state="S",
        stderr_log_dir=tmp_path / "logs",
        timeout_seconds=60.0,
    )
    sup.start()
    grandchild_pid: int | None = None
    try:
        for _ in range(100):  # wait up to ~5s for the grandchild PID
            if sidecar.exists() and sidecar.read_text().strip():
                break
            time.sleep(0.05)
        grandchild_pid = int(sidecar.read_text().strip())
        os.kill(grandchild_pid, 0)  # alive now — raises if not

        sup.terminate()

        # Poll: init should reap the killed grandchild within a few seconds.
        deadline = time.time() + 5.0
        gone = False
        while time.time() < deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                gone = True
                break
        assert gone, (
            f"grandchild {grandchild_pid} survived terminate() — "
            "the driver tree leaked"
        )
    finally:
        # Belt-and-suspenders: never leak the stand-in if the assertion fails.
        if grandchild_pid is not None:
            try:
                os.kill(grandchild_pid, 9)
            except ProcessLookupError:
                pass
        sup.terminate()


# ---- _build_webauto_argv (KEPT) --------------------------------------------


from schwab_cli.auth_handlers import _build_webauto_argv


def test_build_webauto_argv_includes_url_passthrough(tmp_path):
    argv, _log = _build_webauto_argv(
        base_command=("webauto-cli", "script.py"),
        auth_url="https://schwab/auth?x=1",
        extra_flags=["--no-notify", "--state", "S"],
        stderr_log_dir=tmp_path / "logs",
    )
    assert "-a" in argv
    a_values = [argv[i + 1] for i, t in enumerate(argv) if t == "-a"]
    assert any(v == "URL=https://schwab/auth?x=1" for v in a_values)


def test_build_webauto_argv_includes_extra_flags(tmp_path):
    argv, _ = _build_webauto_argv(
        base_command=("webauto-cli",),
        auth_url="https://schwab/auth",
        extra_flags=["--no-notify", "--state", "MY_STATE"],
        stderr_log_dir=tmp_path / "logs",
    )
    assert "--no-notify" in argv
    assert "--state" in argv
    assert argv[argv.index("--state") + 1] == "MY_STATE"


@pytest.mark.parametrize("debug", ["1", None])
def test_build_webauto_argv_always_logs(tmp_path, monkeypatch, debug):
    # --log is now unconditional (every auto-login leaves a trace), so the
    # daemon's DEBUG-less runs are diagnosable too.
    if debug is None:
        monkeypatch.delenv("DEBUG", raising=False)
    else:
        monkeypatch.setenv("DEBUG", debug)
    argv, log_path = _build_webauto_argv(
        base_command=("webauto-cli",),
        auth_url="https://schwab/auth",
        extra_flags=["--no-notify"],
        stderr_log_dir=tmp_path / "logs",
    )
    assert "--log" in argv
    assert argv[argv.index("--log") + 1] == str(log_path)
    assert log_path.parent == tmp_path / "logs"
    assert log_path.name.startswith("auto_login-")


def test_build_webauto_argv_prunes_old_logs(tmp_path):
    from schwab_cli.auth_handlers import _AUTH_LOG_KEEP

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Seed more than the cap with sortable timestamped names.
    for i in range(_AUTH_LOG_KEEP + 5):
        (log_dir / f"auto_login-2026010100{i:04d}.stderr.log").write_text("x")
    _build_webauto_argv(
        base_command=("webauto-cli",),
        auth_url="https://schwab/auth",
        extra_flags=[],
        stderr_log_dir=log_dir,
    )
    remaining = sorted(log_dir.glob("auto_login-*.stderr.log"))
    # Pruned to the cap (the new run's own file is created after the prune,
    # so total is cap + 1 — the cap kept plus the fresh one).
    assert len(remaining) <= _AUTH_LOG_KEEP + 1
    # The oldest seeds were the ones removed.
    assert not (log_dir / "auto_login-2026010100000.stderr.log").exists()


# ---- _terminate (KEPT) -----------------------------------------------------


from schwab_cli.auth_handlers import _terminate
import subprocess


def test_terminate_returns_cleanly_on_already_exited_process():
    """_terminate must be idempotent — no-op on an already-exited process."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    proc.wait()
    # Should not raise even though the process has already exited.
    _terminate(proc)


def test_terminate_does_not_groupkill_a_shared_group(tmp_path):
    """Defensive: when the child is NOT its own group leader (no
    start_new_session), ``_terminate`` must fall back to single-process
    signals — never ``killpg`` the shared group, which would also signal
    the daemon / test-runner itself. (If it group-killed, this very test
    process would die instead of asserting.)"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    # Shares the test-runner's process group — not its own leader.
    assert os.getpgid(proc.pid) != proc.pid
    _terminate(proc)
    assert proc.poll() is not None  # the single process was terminated
