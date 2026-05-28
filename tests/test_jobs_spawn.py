"""TDD red-phase tests for spawn_worker in schwab_cli.server.jobs.runner.

All imports are expected to fail until runner.py gains the spawn_worker /
JobHandle additions.  No real processes are spawned — subprocess.Popen is
monkeypatched throughout.

Run with:
    uv run --frozen --extra dev python -m pytest tests/test_jobs_spawn.py -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from schwab_cli.server.jobs.config import JobConfig
from schwab_cli.server.jobs.runner import JobHandle, spawn_worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(
    job_id: str = "j1",
    command: tuple[str, ...] = ("dataset", "update"),
    enabled: bool = True,
    retries: int = 1,
    retry_delay_s: int = 120,
    timeout_s: int = 3600,
) -> JobConfig:
    return JobConfig(
        id=job_id,
        name="Test Job",
        enabled=enabled,
        cron="*/5 * * * *",
        timezone="UTC",
        type="command",
        command=command,
        retries=retries,
        retry_delay_s=retry_delay_s,
        timeout_s=timeout_s,
    )


# Capture the real class at import time: several tests build the fake *inside*
# a factory that is already installed as subprocess.Popen, so spec=subprocess.Popen
# would otherwise be taken from the patched function (which has no poll/pid).
_REAL_POPEN = subprocess.Popen


def _fake_popen(pid: int = 9999, pgid: int | None = None) -> MagicMock:
    """Return a MagicMock that pretends to be a subprocess.Popen instance."""
    mock = MagicMock(spec=_REAL_POPEN)
    mock.pid = pid
    mock.poll.return_value = None  # still running by default
    return mock


# ---------------------------------------------------------------------------
# JobHandle contract
# ---------------------------------------------------------------------------


def test_spawn_worker_returns_job_handle(tmp_path, monkeypatch):
    """spawn_worker must return a JobHandle instance."""
    fake = _fake_popen(pid=9001)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    # Patch os.getpgid so we don't need a real process
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    handle = spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    assert isinstance(handle, JobHandle)


def test_spawn_worker_pid_matches_popen_pid(tmp_path, monkeypatch):
    """JobHandle.pid must equal the Popen instance's pid."""
    fake = _fake_popen(pid=9002)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    handle = spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    assert handle.pid == 9002


def test_spawn_worker_poll_delegates_to_popen(tmp_path, monkeypatch):
    """handle.poll() must delegate to the underlying Popen.poll()."""
    fake = _fake_popen(pid=9003)
    fake.poll.return_value = None  # still running
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    handle = spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    assert handle.poll() is None

    fake.poll.return_value = 0
    assert handle.poll() == 0


def test_spawn_worker_uses_start_new_session(tmp_path, monkeypatch):
    """Popen must be called with start_new_session=True."""
    captured_kwargs: dict = {}

    def _fake_popen_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        fake = _fake_popen(pid=9004)
        return fake

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    assert captured_kwargs.get("start_new_session") is True


def test_spawn_worker_argv_uses_binary_jobs_run_id(tmp_path, monkeypatch):
    """
    spawn_worker must build argv as [binary, "jobs", "run", cfg.id].
    """
    captured_argv: list[list[str]] = []

    def _fake_popen_factory(*args, **kwargs):
        # args[0] is the argv list
        captured_argv.append(list(args[0]))
        fake = _fake_popen(pid=9005)
        return fake

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    binary = "/usr/local/bin/schwab"
    cfg = _cfg("dataset-update")
    log_path = tmp_path / "dataset-update.log"
    spawn_worker(cfg, log_path=log_path, binary=binary)

    assert len(captured_argv) == 1
    assert captured_argv[0] == [binary, "jobs", "run", "dataset-update"]


def test_spawn_worker_no_binary_uses_resolve_binary(tmp_path, monkeypatch):
    """When binary=None, spawn_worker must call resolve_binary() for the path."""
    from schwab_cli.server.jobs import runner as runner_mod

    resolve_calls: list[None] = []

    def _fake_resolve():
        resolve_calls.append(None)
        return "/fake/schwab"

    monkeypatch.setattr(runner_mod, "resolve_binary", _fake_resolve)

    captured_argv: list[list[str]] = []

    def _fake_popen_factory(*args, **kwargs):
        captured_argv.append(list(args[0]))
        return _fake_popen(pid=9006)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    spawn_worker(_cfg("j1"), log_path=log_path, binary=None)

    assert resolve_calls, "resolve_binary() must be called when binary is None"
    assert captured_argv[0][0] == "/fake/schwab"


def test_spawn_worker_opens_log_path_for_writing(tmp_path, monkeypatch):
    """log_path must be opened (created) for writing."""
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _fake_popen(pid=9007))
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "subdir" / "job.log"
    spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    # The log file (or its parent dir) must exist after spawn_worker returns.
    # The implementation must create the parent dir and open the file.
    assert log_path.parent.exists() or log_path.exists(), (
        "spawn_worker must create the log_path directory/file"
    )


def test_spawn_worker_log_passed_as_stdout_and_stderr(tmp_path, monkeypatch):
    """
    The opened log file must be passed as stdout=log, stderr=STDOUT to Popen
    (so both streams land in the same log).
    """
    captured_kwargs: dict = {}

    def _fake_popen_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_popen(pid=9008)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    # stdout must be a file-like object (not None / PIPE)
    assert captured_kwargs.get("stdout") is not None
    stdout = captured_kwargs["stdout"]
    assert hasattr(stdout, "write"), "stdout must be a writable file object"

    # stderr must be subprocess.STDOUT
    assert captured_kwargs.get("stderr") == subprocess.STDOUT


def test_spawn_worker_closes_parent_log_fd(tmp_path, monkeypatch):
    """The parent's log file handle must be closed after spawn_worker returns.

    The child inherits its own copy of the fd; leaving the parent's copy open
    leaks a descriptor per spawned job. We wrap builtins.open to capture the
    file object handed to Popen and assert it is closed on return.
    """
    import builtins

    from schwab_cli.server.jobs import runner as runner_mod

    real_open = builtins.open
    opened: list = []

    def _tracking_open(*args, **kwargs):
        f = real_open(*args, **kwargs)
        opened.append(f)
        return f

    monkeypatch.setattr(runner_mod, "open", _tracking_open, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _fake_popen(pid=9100))
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    log_path = tmp_path / "j1.log"
    spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")

    assert opened, "spawn_worker must open the log file"
    assert opened[-1].closed, "parent's log file handle must be closed after spawn"


def test_job_handle_terminate_sends_to_process_group(tmp_path, monkeypatch):
    """handle.terminate() must send SIGTERM to the process group."""
    import os
    import signal

    fake = _fake_popen(pid=9009)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    monkeypatch.setattr("os.getpgid", lambda pid: 9009)

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kill_calls.append((pgid, sig)))

    log_path = tmp_path / "j1.log"
    handle = spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")
    handle.terminate()

    assert any(sig == signal.SIGTERM for _, sig in kill_calls), (
        "terminate() must send SIGTERM"
    )


def test_job_handle_kill_sends_sigkill_to_process_group(tmp_path, monkeypatch):
    """handle.kill() must send SIGKILL to the process group."""
    import os
    import signal

    fake = _fake_popen(pid=9010)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    monkeypatch.setattr("os.getpgid", lambda pid: 9010)

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kill_calls.append((pgid, sig)))

    log_path = tmp_path / "j1.log"
    handle = spawn_worker(_cfg("j1"), log_path=log_path, binary="/usr/local/bin/schwab")
    handle.kill()

    assert any(sig == signal.SIGKILL for _, sig in kill_calls), (
        "kill() must send SIGKILL"
    )
