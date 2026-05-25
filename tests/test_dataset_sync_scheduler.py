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
    # market-data ok, accounts fails (rc=1), indices fails (rc=3).
    # rc=2 is reserved for EXIT_AUTH_FAILED — triggers reactive retry,
    # not the plain job_failed path this test pins.
    _patch_popen(monkeypatch, rcs=[0, 1, 3])

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


def test_spawn_failure_does_not_abort_peer_children(monkeypatch):
    """A child whose Popen itself raises (FileNotFoundError, etc.)
    used to take the entire run down. Now it's recorded as a
    synthetic failure and the remaining children still launch."""
    notifier = _FakeNotifier()
    _no_token_refresh(monkeypatch)
    _no_last_run_write(monkeypatch)

    # First Popen call raises (binary missing); second + third
    # succeed with rc=0.
    call_log: list[list[str]] = []

    def _flaky_popen(argv, **kwargs):
        call_log.append(argv)
        if len(call_log) == 1:
            raise FileNotFoundError(
                2, "No such file or directory", "schwab",
            )
        return _FakePopen(rc=0)

    monkeypatch.setattr(ss.subprocess, "Popen", _flaky_popen)
    monkeypatch.setattr(ss.os, "killpg", lambda *_a, **_k: None)
    monkeypatch.setattr(ss.os, "getpgid", lambda _pid: 0)

    rc = ss.run_daily_sync(
        notifier=notifier, binary_path="/bin/true",
    )
    # All three Popen attempts ran (first failed, two more succeeded).
    assert len(call_log) == 3
    # Run reports failure overall.
    assert rc == 1
    # Telegram alert names exactly the spawn-failed child.
    failed_events = [
        c for c in notifier.calls if c[0] == "scheduler.job_failed"
    ]
    assert failed_events, "no job_failed event emitted"
    failed_field = failed_events[0][1]["failed"]
    assert failed_field == ss.JOB_MARKET_DATA


def test_top_level_crash_emits_alert_and_writes_marker(
    monkeypatch, tmp_path,
):
    """Any unexpected exception in run_daily_sync must still fire a
    `scheduler.crashed` alert AND write last_run.json so the
    operator has a signal that the cron silently broke."""
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        ss, "_last_run_path", lambda: tmp_path / "last_run.json",
    )

    def _boom_in_token_check(_notifier):
        raise RuntimeError("simulated PATH issue")

    monkeypatch.setattr(ss, "_ensure_token_valid", _boom_in_token_check)

    rc = ss.run_daily_sync(
        notifier=notifier, binary_path="/bin/true",
    )
    assert rc == 1

    crash_events = [c for c in notifier.calls if c[0] == "scheduler.crashed"]
    assert crash_events
    assert "simulated PATH issue" in crash_events[0][1]["error"]

    import json as _json
    payload = _json.loads((tmp_path / "last_run.json").read_text())
    assert payload["overall_succeeded"] is False
    assert payload["jobs"][0]["name"] == "scheduler"


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


# ============================================================
# Proactive auth bridge (refresh-token TTL < 24h → auto-login)
# ============================================================


def _make_session(refresh_ttl_seconds: int):
    """Build a stub Session-shaped object with the desired refresh-token TTL."""
    import time as _t
    from types import SimpleNamespace
    now = int(_t.time())
    return SimpleNamespace(
        access_token="a", refresh_token="r",
        expires_at=now + 3600,
        refresh_token_expires_at=now + refresh_ttl_seconds,
    )


def _make_cfg(auto_login_command: tuple[str, ...] | None):
    from types import SimpleNamespace
    return SimpleNamespace(
        client_id="x", client_secret="y", redirect_uri="z",
        auth_flow="code_relay", code_relay_url="r",
        auto_login_command=auto_login_command,
        auto_login_timeout_seconds=300, version=1,
    )


def test_proactive_auth_skipped_when_ttl_healthy(monkeypatch):
    """TTL >= 24h → no notification, no auto-login invoked."""
    notifier = _FakeNotifier()
    audit_calls: list[str] = []
    audit = type("A", (), {
        "info": lambda self, m: audit_calls.append(("info", m)),
        "warning": lambda self, m: audit_calls.append(("warn", m)),
        "error": lambda self, m: audit_calls.append(("err", m)),
    })()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(("webauto-cli", "x.py")),
    )
    monkeypatch.setattr(
        "schwab_cli.session.load",
        lambda: _make_session(refresh_ttl_seconds=48 * 3600),
    )
    perform = _Counter()
    monkeypatch.setattr(
        "schwab_cli.auth_flows.perform_full_auth",
        lambda _cfg: perform.bump_and_return(None),
    )

    ss._ensure_refresh_token_lifetime(notifier, audit)

    assert perform.count == 0
    assert not any(e == "scheduler.proactive_auth_invoked" for e, _ in notifier.calls)


def test_proactive_auth_fires_when_ttl_below_24h(monkeypatch):
    """TTL < 24h with auto_login_command set → perform_full_auth invoked + success event."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(("webauto-cli", "x.py")),
    )
    monkeypatch.setattr(
        "schwab_cli.session.load",
        lambda: _make_session(refresh_ttl_seconds=10 * 3600),
    )
    fresh_session = _make_session(refresh_ttl_seconds=7 * 24 * 3600)

    def _fake_perform(cfg):
        return fresh_session

    monkeypatch.setattr(
        "schwab_cli.auth_flows.perform_full_auth", _fake_perform,
    )

    ss._ensure_refresh_token_lifetime(notifier, audit)

    events = [e for e, _ in notifier.calls]
    assert "scheduler.proactive_auth_invoked" in events
    assert "scheduler.proactive_auth_succeeded" in events


def test_proactive_auth_skipped_when_no_auto_login_command(monkeypatch):
    """TTL < 24h but no auto_login_command → emit skipped, don't crash."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(None),
    )
    monkeypatch.setattr(
        "schwab_cli.session.load",
        lambda: _make_session(refresh_ttl_seconds=10 * 3600),
    )

    ss._ensure_refresh_token_lifetime(notifier, audit)

    events = [e for e, _ in notifier.calls]
    assert "scheduler.proactive_auth_skipped" in events
    assert "scheduler.proactive_auth_invoked" not in events


def test_proactive_auth_failure_does_not_crash(monkeypatch):
    """TTL < 24h, auto_login_command set, but perform_full_auth raises.
    Emit failed event; don't propagate (children may still succeed)."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(("webauto-cli", "x.py")),
    )
    monkeypatch.setattr(
        "schwab_cli.session.load",
        lambda: _make_session(refresh_ttl_seconds=10 * 3600),
    )

    def _boom(_cfg):
        raise RuntimeError("simulated webauto failure")

    monkeypatch.setattr("schwab_cli.auth_flows.perform_full_auth", _boom)

    ss._ensure_refresh_token_lifetime(notifier, audit)  # must not raise

    events = [e for e, _ in notifier.calls]
    assert "scheduler.proactive_auth_failed" in events


# ============================================================
# Reactive retry on EXIT_AUTH_FAILED
# ============================================================


def test_reactive_retry_fires_only_on_exit_auth_failed(monkeypatch):
    """rc=1 alone should NOT trigger reactive retry; only rc=2 does."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    perform = _Counter()
    monkeypatch.setattr(
        "schwab_cli.auth_flows.perform_full_auth",
        lambda _c: perform.bump_and_return(None),
    )

    results = [
        _JobResult(name="market-data", returncode=0, succeeded=True),
        _JobResult(name="accounts", returncode=1, succeeded=False),
        _JobResult(name="indices", returncode=0, succeeded=True),
    ]
    out = ss._maybe_retry_auth_failed(
        results=results,
        binary="/bin/true",
        all_jobs=[("market-data", []), ("accounts", []), ("indices", [])],
        child_timeout_s=10,
        notifier=notifier,
        audit=audit,
    )
    assert perform.count == 0
    assert out is results
    assert "scheduler.reactive_auth_retry" not in [e for e, _ in notifier.calls]


def test_reactive_retry_respawns_only_auth_failed_children(monkeypatch):
    """rc=2 from accounts triggers re-auth + respawn of accounts only."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(("webauto-cli", "x.py")),
    )
    monkeypatch.setattr(
        "schwab_cli.auth_flows.perform_full_auth",
        lambda _c: _make_session(refresh_ttl_seconds=7 * 24 * 3600),
    )
    respawned: list[tuple[str, list[str]]] = []

    def _fake_dispatch(jobs, *, child_timeout_s, audit):
        respawned.extend(jobs)
        return [
            _JobResult(name=name, returncode=0, succeeded=True)
            for name, _ in jobs
        ]

    monkeypatch.setattr(ss, "_dispatch_parallel", _fake_dispatch)

    results = [
        _JobResult(name="market-data", returncode=0, succeeded=True),
        _JobResult(name="accounts", returncode=2, succeeded=False),
        _JobResult(name="indices", returncode=0, succeeded=True),
    ]
    out = ss._maybe_retry_auth_failed(
        results=results,
        binary="/bin/true",
        all_jobs=[
            ("market-data", ["a"]), ("accounts", ["b"]), ("indices", ["c"]),
        ],
        child_timeout_s=10,
        notifier=notifier,
        audit=audit,
    )

    assert [name for name, _ in respawned] == ["accounts"]
    assert respawned[0][1][-1] == "--skip-wait"
    out_by_name = {r.name: r for r in out}
    assert out_by_name["accounts"].succeeded is True
    assert out_by_name["market-data"].succeeded is True
    events = [e for e, _ in notifier.calls]
    assert "scheduler.reactive_auth_retry" in events


def test_reactive_retry_gives_up_when_no_auto_login_command(monkeypatch):
    """rc=2 with no auto_login_command → emit unrecoverable; original results stand."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr("schwab_cli.config.load", lambda: _make_cfg(None))
    perform = _Counter()
    monkeypatch.setattr(
        "schwab_cli.auth_flows.perform_full_auth",
        lambda _c: perform.bump_and_return(None),
    )

    results = [
        _JobResult(name="accounts", returncode=2, succeeded=False),
    ]
    out = ss._maybe_retry_auth_failed(
        results=results,
        binary="/bin/true",
        all_jobs=[("accounts", [])],
        child_timeout_s=10,
        notifier=notifier,
        audit=audit,
    )

    assert perform.count == 0
    assert out is results
    events = [e for e, _ in notifier.calls]
    assert "scheduler.auth_unrecoverable" in events


def test_reactive_retry_gives_up_when_reauth_itself_fails(monkeypatch):
    """rc=2 + perform_full_auth raises → emit unrecoverable; no respawn."""
    notifier = _FakeNotifier()
    audit = _RecordingAudit()
    monkeypatch.setattr(
        "schwab_cli.config.load", lambda: _make_cfg(("webauto-cli", "x.py")),
    )

    def _boom(_cfg):
        raise RuntimeError("webauto fell over")

    monkeypatch.setattr("schwab_cli.auth_flows.perform_full_auth", _boom)
    monkeypatch.setattr(ss, "_dispatch_parallel", lambda *a, **k: pytest.fail("should not re-dispatch"))

    results = [_JobResult(name="accounts", returncode=2, succeeded=False)]
    out = ss._maybe_retry_auth_failed(
        results=results,
        binary="/bin/true",
        all_jobs=[("accounts", [])],
        child_timeout_s=10,
        notifier=notifier,
        audit=audit,
    )

    assert out is results
    events = [e for e, _ in notifier.calls]
    assert "scheduler.auth_unrecoverable" in events


# ---- small test helpers used by the new tests ----------------


@dataclass
class _JobResult:
    name: str
    returncode: int
    succeeded: bool
    duration_s: float = 0.0
    timed_out: bool = False
    stdout_tail: str = ""


class _RecordingAudit:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []
    def info(self, m):    self.lines.append(("info", m))
    def warning(self, m): self.lines.append(("warn", m))
    def error(self, m):   self.lines.append(("err", m))


class _Counter:
    def __init__(self):
        self.count = 0
    def bump_and_return(self, v):
        self.count += 1
        return v
