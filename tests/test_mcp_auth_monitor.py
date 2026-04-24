"""Tests for the proactive auth monitor.

All subprocess work is mocked via an injected runner; the clock is
injected too so we can jump forward through the threshold/anti-
thrash windows without real waits.
"""

from __future__ import annotations

import asyncio
import io

from schwab_cli.mcp_server.auth_monitor import (
    AuthMonitor,
    AuthMonitorResult,
)
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.notify import Notifier
from schwab_cli.notify import config as notify_config
from schwab_cli.session import Session


def _make_session(refresh_expires_at: int) -> Session:
    return Session(
        access_token="at",
        refresh_token="rt",
        expires_at=refresh_expires_at,
        refresh_token_expires_at=refresh_expires_at,
    )


def _make_notifier(buf: io.StringIO | None = None) -> Notifier:
    return Notifier(
        notify_config.NotificationConfig(),
        logbook=LogBook(stream=buf or io.StringIO()),
    )


def _make_monitor(
    *,
    session_refresh_expires_at: int,
    now: int,
    runner_result: tuple[int, str] = (0, ""),
    runner_side_effect: Exception | None = None,
) -> tuple[AuthMonitor, list[dict], io.StringIO]:
    """Build a monitor primed with a stubbed clock, session loader,
    and subprocess runner. Returns ``(monitor, runner_calls, logbuf)``
    — ``runner_calls`` is a list the fake runner appends to on each
    invocation."""
    buf = io.StringIO()
    logbook = LogBook(stream=buf)
    notifier = _make_notifier(buf)

    runner_calls: list[dict] = []

    async def fake_runner(cmd, *, env, timeout):
        runner_calls.append({"cmd": cmd, "timeout": timeout})
        if runner_side_effect is not None:
            raise runner_side_effect
        return runner_result

    session = _make_session(session_refresh_expires_at)

    clock_state = [float(now)]

    def clock():
        return clock_state[0]

    mon = AuthMonitor(
        logbook, notifier,
        threshold_seconds=3600,
        anti_thrash_seconds=3600,
        poll_seconds=60,
        subprocess_timeout_seconds=120,
        warn_at_seconds=900,
        session_loader=lambda: session,
        subprocess_runner=fake_runner,
        clock=clock,
    )
    # Expose the clock-state list so tests can advance time.
    mon._clock_state = clock_state  # type: ignore[attr-defined]
    return mon, runner_calls, buf


# ---- run_once: happy path ---------------------------------------------


def test_run_once_success_emits_success_event():
    mon, runner_calls, buf = _make_monitor(
        session_refresh_expires_at=1000,
        now=500,
    )
    result = asyncio.run(mon.run_once(reason="test"))
    assert result.ok is True
    assert len(runner_calls) == 1
    assert runner_calls[0]["cmd"] == ["schwab_cli", "auth", "--force"]
    assert "auth.auto_login.succeeded" in buf.getvalue()


def test_run_once_failure_emits_failed_event_with_stderr_tail():
    mon, _, buf = _make_monitor(
        session_refresh_expires_at=1000,
        now=500,
        runner_result=(1, "line1\nline2\nselenium: 401 unauthorized\n"),
    )
    result = asyncio.run(mon.run_once(reason="test"))
    assert result.ok is False
    assert "401" in result.stderr_tail
    assert "auth.auto_login.failed" in buf.getvalue()


def test_run_once_subprocess_exception_is_captured():
    mon, _, buf = _make_monitor(
        session_refresh_expires_at=1000,
        now=500,
        runner_side_effect=RuntimeError("boom"),
    )
    result = asyncio.run(mon.run_once(reason="test"))
    assert result.ok is False
    assert "RuntimeError" in result.stderr_tail
    assert "auth.auto_login.failed" in buf.getvalue()


# ---- on_rotation_success hook -----------------------------------------


def test_on_rotation_success_hook_fires():
    buf = io.StringIO()
    logbook = LogBook(stream=buf)
    notifier = _make_notifier(buf)

    async def fake_runner(cmd, *, env, timeout):
        return 0, ""

    hook_called = []

    async def on_success():
        hook_called.append(True)

    mon = AuthMonitor(
        logbook, notifier,
        session_loader=lambda: _make_session(1000),
        subprocess_runner=fake_runner,
        clock=lambda: 500.0,
        on_rotation_success=on_success,
    )
    result = asyncio.run(mon.run_once(reason="test"))
    assert result.ok is True
    assert hook_called == [True]


def test_on_rotation_success_hook_error_is_logged_not_raised():
    buf = io.StringIO()
    logbook = LogBook(stream=buf)
    notifier = _make_notifier(buf)

    async def fake_runner(cmd, *, env, timeout):
        return 0, ""

    async def broken_hook():
        raise RuntimeError("broken")

    mon = AuthMonitor(
        logbook, notifier,
        session_loader=lambda: _make_session(1000),
        subprocess_runner=fake_runner,
        clock=lambda: 500.0,
        on_rotation_success=broken_hook,
    )
    # Must not raise.
    result = asyncio.run(mon.run_once(reason="test"))
    assert result.ok is True
    assert "on_rotation_hook_error" in buf.getvalue()


# ---- _tick behaviour ---------------------------------------------------


def test_tick_triggers_rotation_when_within_threshold():
    """At 30 min remaining (< 1 h threshold), a tick should fire a
    rotation attempt."""
    mon, runner_calls, _ = _make_monitor(
        session_refresh_expires_at=2000,
        now=200,  # 1800s remaining
    )
    asyncio.run(mon._tick())
    assert len(runner_calls) == 1


def test_tick_does_not_trigger_when_far_from_expiry():
    mon, runner_calls, _ = _make_monitor(
        session_refresh_expires_at=10_000,
        now=200,  # ~2h45m remaining
    )
    asyncio.run(mon._tick())
    assert runner_calls == []


def test_anti_thrash_blocks_repeat_attempts_within_window():
    mon, runner_calls, _ = _make_monitor(
        session_refresh_expires_at=2000,
        now=200,
        runner_result=(1, "fail"),  # failure keeps anti-thrash ticking
    )
    asyncio.run(mon._tick())
    assert len(runner_calls) == 1
    # Advance 10 minutes — well inside the 1h anti-thrash window.
    mon._clock_state[0] = 800.0  # type: ignore[attr-defined]
    asyncio.run(mon._tick())
    assert len(runner_calls) == 1  # blocked
    # Advance past the anti-thrash window.
    mon._clock_state[0] = 4000.0  # type: ignore[attr-defined]
    asyncio.run(mon._tick())
    assert len(runner_calls) == 2


def test_15min_warning_fires_once_when_anti_thrash_blocked():
    """In the danger zone (< 15 min remaining) with anti-thrash
    still active, the monitor should emit an `auth.refresh_expiring`
    warning to grab the user's attention."""
    mon, _, buf = _make_monitor(
        session_refresh_expires_at=2000,
        now=200,
        runner_result=(1, "fail"),
    )
    # First tick: attempt + fail.
    asyncio.run(mon._tick())
    # Jump into the 15-min danger zone; anti-thrash still active.
    mon._clock_state[0] = 1500.0  # type: ignore[attr-defined]  # 500s remaining
    asyncio.run(mon._tick())
    log = buf.getvalue()
    assert "auth.refresh_expiring" in log


def test_tick_with_no_session_is_noop():
    buf = io.StringIO()
    logbook = LogBook(stream=buf)
    notifier = _make_notifier(buf)

    runner_calls: list = []

    async def fake_runner(cmd, *, env, timeout):
        runner_calls.append(cmd)
        return 0, ""

    mon = AuthMonitor(
        logbook, notifier,
        session_loader=lambda: None,
        subprocess_runner=fake_runner,
        clock=lambda: 500.0,
    )
    asyncio.run(mon._tick())
    assert runner_calls == []


# ---- start / stop ------------------------------------------------------


def test_disabled_monitor_does_not_start_task():
    buf = io.StringIO()
    mon = AuthMonitor(
        LogBook(stream=buf),
        _make_notifier(buf),
        enabled=False,
        session_loader=lambda: _make_session(1000),
    )
    mon.start()
    # No task created.
    assert mon._task is None  # noqa: SLF001


def test_start_is_idempotent():
    async def run():
        buf = io.StringIO()

        async def fake_runner(*a, **k):
            return 0, ""

        mon = AuthMonitor(
            LogBook(stream=buf),
            _make_notifier(buf),
            poll_seconds=1,
            session_loader=lambda: _make_session(10_000),
            subprocess_runner=fake_runner,
            clock=lambda: 100.0,
        )
        mon.start()
        t1 = mon._task
        mon.start()
        t2 = mon._task
        assert t1 is t2
        await mon.stop()

    asyncio.run(run())


# ---- data class --------------------------------------------------------


def test_result_dataclass_defaults():
    r = AuthMonitorResult(ok=True)
    assert r.ok is True
    assert r.stderr_tail == ""
    assert r.duration_sec == 0.0
