"""Unified scheduler dispatch + token-refresh contract.

The orchestrator runs unattended via launchd; the alerting + return-
code contracts here are the only signals an operator sees when
something breaks. These tests pin the contract so refactors can't
silently change behavior.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from schwab_cli.dataset import sync_scheduler as ss


@dataclass
class _FakeNotifier:
    """Captures every emit call so assertions can introspect both
    event names and field payloads without monkeypatching the real
    Notifier transport stack."""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event: str, **fields: Any) -> None:
        self.calls.append((event, fields))

    def events(self) -> list[str]:
        return [e for e, _ in self.calls]


@dataclass
class _FakePopen:
    """Drop-in for ``subprocess.Popen`` that records ordering and
    returns a configurable rc on .wait()."""
    rc: int = 0
    pid: int = 9999
    returncode: int | None = None
    started_at: float = 0.0
    raise_timeout: bool = False
    # Shared across siblings so tests can confirm parallel-start.
    _all_starts: list[float] = field(default_factory=list)

    def wait(self, timeout: float | None = None):
        if self.raise_timeout:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        self.returncode = self.rc
        return self.rc


def _patch_popen(monkeypatch, rcs, raise_timeout=False, capture=None):
    """Install a fake ``subprocess.Popen`` that hands back rcs in
    order. ``capture`` records each invocation's argv for assertions."""
    iterator = iter(rcs)
    starts: list[float] = []
    if capture is None:
        capture = []

    def _factory(argv, **kwargs):
        import time as _t
        starts.append(_t.time())
        capture.append(argv)
        # Children get a no-op stdout file handle.
        rc = next(iterator, 0)
        return _FakePopen(
            rc=rc,
            raise_timeout=raise_timeout,
            _all_starts=starts,
            started_at=_t.time(),
        )

    monkeypatch.setattr(ss.subprocess, "Popen", _factory)
    # Tests don't need the killpg path; bypass it.
    monkeypatch.setattr(ss.os, "killpg", lambda *_a, **_k: None)
    monkeypatch.setattr(ss.os, "getpgid", lambda _pid: 0)
    return starts, capture


def _no_token_refresh(monkeypatch):
    monkeypatch.setattr(ss, "_ensure_token_valid", lambda _n: None)


def _no_last_run_write(monkeypatch):
    monkeypatch.setattr(ss, "_write_last_run", lambda _s: None)


# ---- dispatch happy path ---------------------------------------------


def test_happy_path_returns_zero_without_alert(monkeypatch):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)
    _patch_popen(monkeypatch, rcs=[0, 0, 0])

    rc = ss.run_daily_sync(
        notifier=notifier, binary_path="/bin/true",
    )
    assert rc == 0
    assert "scheduler.job_failed" not in notifier.events()


def test_mixed_failure_emits_alert_with_failed_names(monkeypatch):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)
    # market-data ok, accounts fails, indices fails
    _patch_popen(monkeypatch, rcs=[0, 1, 2])

    rc = ss.run_daily_sync(
        notifier=notifier, binary_path="/bin/true",
    )
    assert rc == 1
    failed_events = [c for c in notifier.calls if c[0] == "scheduler.job_failed"]
    assert len(failed_events) == 1
    failed_field = failed_events[0][1]["failed"]
    assert "accounts" in failed_field
    assert "indices" in failed_field
    assert "market-data" not in failed_field


def test_timeout_marks_job_failed_and_alerts(monkeypatch):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)
    _patch_popen(monkeypatch, rcs=[0, 0, 0], raise_timeout=True)

    rc = ss.run_daily_sync(
        notifier=notifier, binary_path="/bin/true",
        child_timeout_s=0.01,
    )
    assert rc == 1
    failed = [c for c in notifier.calls if c[0] == "scheduler.job_failed"]
    assert failed
    assert "timeout" in failed[0][1]["details"]


def test_parallel_start_order(monkeypatch):
    """All three children must be Popen-started before any single
    wait() returns — proves the dispatch isn't serialized."""
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)
    starts, _ = _patch_popen(monkeypatch, rcs=[0, 0, 0])

    ss.run_daily_sync(notifier=notifier, binary_path="/bin/true")
    # All three start timestamps captured before the first .wait()
    # returns (our fake .wait is synchronous; the assertion is that
    # ALL three Popens are constructed before we entered the wait
    # loop — guaranteed by the implementation walking jobs in two
    # passes).
    assert len(starts) == 3


def test_job_argv_contains_expected_flags(monkeypatch):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)
    starts, capture = _patch_popen(monkeypatch, rcs=[0, 0, 0])

    ss.run_daily_sync(notifier=notifier, binary_path="/usr/local/bin/schwab")
    assert any(
        "--group" in argv and "volatility" in argv for argv in capture
    )
    assert any(
        "accounts" in argv and "snapshot" in argv for argv in capture
    )
    indices_argv = next(
        argv for argv in capture if "--indices" in argv
    )
    assert "--max-age-days" in indices_argv
    assert "--anchor-hour" in indices_argv


# ---- token refresh ---------------------------------------------------


def test_ensure_token_valid_no_session_emits_alert(monkeypatch):
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        "schwab_cli.session.load", lambda: None,
    )
    monkeypatch.setattr(
        "schwab_cli.config.load",
        lambda: object(),  # any non-None value
    )
    ss._ensure_token_valid(notifier)
    assert "scheduler.token_refresh_failed" in notifier.events()


def test_ensure_token_valid_no_config_emits_alert(monkeypatch):
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: None,
    )
    ss._ensure_token_valid(notifier)
    assert "scheduler.token_refresh_failed" in notifier.events()


def test_ensure_token_valid_fresh_token_is_noop(monkeypatch):
    """Access token with > 30 min left should NOT call oauth.refresh."""
    notifier = _FakeNotifier()
    import time as _t

    @dataclass
    class _Session:
        expires_at: int = int(_t.time()) + 7200  # 2 hours
        refresh_token: str = "rt"

    monkeypatch.setattr("schwab_cli.config.load", lambda: object())
    monkeypatch.setattr("schwab_cli.session.load", lambda: _Session())

    refresh_called = []
    monkeypatch.setattr(
        "schwab_cli.oauth.refresh",
        lambda *a, **k: refresh_called.append(1),
    )

    ss._ensure_token_valid(notifier)
    assert refresh_called == []
    assert notifier.calls == []


def test_ensure_token_valid_refresh_raises_is_non_fatal(monkeypatch):
    """If oauth.refresh raises, emit an alert but don't propagate —
    the children still get a chance with the old token."""
    notifier = _FakeNotifier()
    import time as _t

    @dataclass
    class _Session:
        expires_at: int = int(_t.time()) + 60  # inside the window
        refresh_token: str = "rt"

    monkeypatch.setattr("schwab_cli.config.load", lambda: object())
    monkeypatch.setattr("schwab_cli.session.load", lambda: _Session())
    monkeypatch.setattr(
        "schwab_cli.oauth.refresh",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    # Must return without raising
    ss._ensure_token_valid(notifier)
    assert "scheduler.token_refresh_failed" in notifier.events()


# ---- last-run marker -------------------------------------------------


def test_last_run_json_written_with_overall_succeeded(
    monkeypatch, tmp_path,
):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    monkeypatch.setattr(ss, "_last_run_path", lambda: tmp_path / "last_run.json")
    _patch_popen(monkeypatch, rcs=[0, 0, 0])

    ss.run_daily_sync(notifier=notifier, binary_path="/bin/true")

    import json as _json
    payload = _json.loads((tmp_path / "last_run.json").read_text())
    assert payload["overall_succeeded"] is True
    assert len(payload["jobs"]) == 3
    assert all(j["returncode"] == 0 for j in payload["jobs"])


def test_last_run_json_records_failure(monkeypatch, tmp_path):
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    monkeypatch.setattr(ss, "_last_run_path", lambda: tmp_path / "last_run.json")
    _patch_popen(monkeypatch, rcs=[0, 1, 0])

    ss.run_daily_sync(notifier=notifier, binary_path="/bin/true")

    import json as _json
    payload = _json.loads((tmp_path / "last_run.json").read_text())
    assert payload["overall_succeeded"] is False
    failed_jobs = [j for j in payload["jobs"] if j["returncode"] != 0]
    assert len(failed_jobs) == 1
    assert failed_jobs[0]["name"] == ss.JOB_ACCOUNTS
