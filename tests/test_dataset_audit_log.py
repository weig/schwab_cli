"""Audit log: structure + integration with the scheduler dispatch
path. Pins the contract that every state transition becomes a line
in ``scheduler.log``."""
from __future__ import annotations

import logging
import time as _time
from pathlib import Path

import pytest

from schwab_cli.dataset import audit_log
from schwab_cli.dataset import sync_scheduler as ss


@pytest.fixture
def audit_path():
    """Return the per-test audit-log path that ``tests/conftest.py``
    already monkeypatched for us. Just resolves the same lambda."""
    return audit_log.audit_log_path()


# ---- format -----------------------------------------------------------


def test_setup_writes_iso8601_utc_timestamp(audit_path):
    log = audit_log.setup()
    log.info("[test] hello")
    for h in log.handlers:
        h.flush()
    line = audit_path.read_text().strip().splitlines()[-1]
    # ISO 8601 with explicit Z suffix.
    assert "Z [test] hello" in line
    # Date prefix matches YYYY-MM-DDTHH:MM:SS
    import re
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ", line,
    )


def test_setup_is_idempotent(audit_path):
    """Calling setup twice must not double-attach the handler — that
    would write each line twice and inflate the audit file."""
    log1 = audit_log.setup()
    log2 = audit_log.setup()
    assert log1 is log2
    log1.info("[test] once")
    for h in log1.handlers:
        h.flush()
    lines = [
        l for l in audit_path.read_text().splitlines()
        if "[test] once" in l
    ]
    assert len(lines) == 1


def test_task_log_prepends_tag(audit_path):
    audit_log.task_log("market-data").info("started")
    for h in logging.getLogger("schwab_cli.audit").handlers:
        h.flush()
    line = audit_path.read_text().strip().splitlines()[-1]
    assert "[market-data] started" in line


# ---- integration with scheduler --------------------------------------


def _fake_run_factory(rc, stderr=""):
    from types import SimpleNamespace
    def _run(*_a, **_k):
        return SimpleNamespace(returncode=rc, stderr=stderr, stdout="")
    return _run


def test_scheduler_writes_full_run_audit_trail(
    monkeypatch, audit_path, tmp_path,
):
    """Run an end-to-end happy-path sync and assert the audit log
    captured every transition: start, scheduled, per-task started,
    per-task finished, summary, finished."""
    import subprocess as _sp
    from dataclasses import dataclass, field

    @dataclass
    class _FakePopen:
        rc: int = 0
        pid: int = 9999
        returncode: int | None = None
        def wait(self, timeout=None):
            self.returncode = self.rc
            return self.rc

    starts: list[list[str]] = []
    def _factory(argv, **kwargs):
        starts.append(argv)
        return _FakePopen()

    monkeypatch.setattr(ss.subprocess, "Popen", _factory)
    monkeypatch.setattr(ss.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(ss.os, "getpgid", lambda _pid: 0)
    monkeypatch.setattr(ss, "_ensure_token_valid", lambda _n: None)
    monkeypatch.setattr(
        ss, "_last_run_path", lambda: tmp_path / "last_run.json",
    )

    class _FakeNotifier:
        def __init__(self): self.calls = []
        def emit(self, e, **k): self.calls.append((e, k))

    rc = ss.run_daily_sync(
        notifier=_FakeNotifier(), binary_path="/bin/true",
    )
    assert rc == 0

    for h in logging.getLogger("schwab_cli.audit").handlers:
        h.flush()
    content = audit_path.read_text()

    # Top-level events present.
    assert "[scheduler] start" in content
    assert "scheduled 3 task(s)" in content
    assert "[scheduler] summary:" in content
    assert "[scheduler] finished" in content
    # Per-task lifecycle for each child.
    for task in ("market-data", "accounts", "indices"):
        assert f"task {task} started" in content
        assert f"task {task} finished" in content


def test_scheduler_audit_records_failure_and_alert_dispatch(
    monkeypatch, audit_path, tmp_path,
):
    """On a failing run, the audit log must record the failure and
    the outcome of the Telegram alert call — that's the offline
    breadcrumb the user can grep when Telegram itself was down."""
    import subprocess as _sp
    from dataclasses import dataclass

    @dataclass
    class _FakePopen:
        rc: int
        pid: int = 9999
        returncode: int | None = None
        def wait(self, timeout=None):
            self.returncode = self.rc
            return self.rc

    rcs = iter([0, 1, 0])  # accounts fails
    monkeypatch.setattr(
        ss.subprocess, "Popen",
        lambda argv, **kw: _FakePopen(rc=next(rcs)),
    )
    monkeypatch.setattr(ss.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(ss.os, "getpgid", lambda _pid: 0)
    monkeypatch.setattr(ss, "_ensure_token_valid", lambda _n: None)
    monkeypatch.setattr(
        ss, "_last_run_path", lambda: tmp_path / "last_run.json",
    )

    class _FakeNotifier:
        def __init__(self): self.calls = []
        def emit(self, e, **k): self.calls.append((e, k))

    rc = ss.run_daily_sync(
        notifier=_FakeNotifier(), binary_path="/bin/true",
    )
    assert rc == 1

    for h in logging.getLogger("schwab_cli.audit").handlers:
        h.flush()
    content = audit_path.read_text()

    assert "1 failed" in content
    # Alert dispatch line names the failed task.
    assert "alert dispatched: scheduler.job_failed" in content
    assert "accounts" in content


def test_scheduler_crash_path_logs_to_audit(
    monkeypatch, audit_path, tmp_path,
):
    """An unexpected exception in the inner run must surface in
    the audit log as a `crashed: …` line — that's how the operator
    discovers a silent failure post-hoc."""
    def _boom(*_a, **_k):
        raise RuntimeError("audit-test boom")

    monkeypatch.setattr(ss, "_ensure_token_valid", _boom)
    monkeypatch.setattr(
        ss, "_last_run_path", lambda: tmp_path / "last_run.json",
    )

    class _FakeNotifier:
        def __init__(self): self.calls = []
        def emit(self, e, **k): self.calls.append((e, k))

    rc = ss.run_daily_sync(
        notifier=_FakeNotifier(), binary_path="/bin/true",
    )
    assert rc == 1

    for h in logging.getLogger("schwab_cli.audit").handlers:
        h.flush()
    content = audit_path.read_text()
    assert "[scheduler] crashed: RuntimeError: audit-test boom" in content
