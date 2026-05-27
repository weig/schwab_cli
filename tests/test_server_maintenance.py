"""Spec-based acceptance tests (TDD red) for schwab_cli.server.maintenance.

These tests will FAIL until the implementation is written — that is expected.
The modules are import-guarded so the file always collects; individual tests
will show as FAILED (not ERROR) once the guard passes, or as XFAIL / skipped
until then.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import guard: collect the file even when the implementation doesn't exist.
# ---------------------------------------------------------------------------
try:
    from schwab_cli.server.maintenance import (
        DEFAULT_INTERVAL_S,
        MaintenanceTick,
        run_loop,
        run_once,
    )
    _MODULE_AVAILABLE = True
except ModuleNotFoundError:
    _MODULE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _MODULE_AVAILABLE,
    reason="schwab_cli.server.maintenance not implemented yet",
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

try:
    from schwab_cli.config import Config
    from schwab_cli.session import Session
except ImportError:
    Config = None  # type: ignore[misc, assignment]
    Session = None  # type: ignore[misc, assignment]

_CFG = Config(
    client_id="cid",
    client_secret="csec",
    redirect_uri="https://127.0.0.1:8443",
) if Config is not None else None

# A fixed epoch that our injected `now` will return.
_NOW = 1_700_000_000


def _session_with_refresh_ttl(ttl: int) -> "Session":
    """Return a Session whose refresh_token_expires_at = _NOW + ttl."""
    return Session(
        access_token="atok",
        refresh_token="rtok",
        expires_at=_NOW + 3600,
        refresh_token_expires_at=_NOW + ttl,
    )


# ---------------------------------------------------------------------------
# Tests for run_once
# ---------------------------------------------------------------------------


class TestRunOnceRenewalPath:
    """ttl <= DEFAULT_INTERVAL_S  →  perform_full_auth is called."""

    def test_tick_action_is_renewed_on_success(self, monkeypatch):
        """When ttl is at the boundary, perform_full_auth is called and the
        returned tick has action == 'renewed'."""
        fresh = _session_with_refresh_ttl(DEFAULT_INTERVAL_S)  # exactly at window
        renewed = _session_with_refresh_ttl(DEFAULT_INTERVAL_S * 2)

        monkeypatch.setattr(
            "schwab_cli.session.load", lambda: fresh,
        )
        with pytest.MonkeyPatch.context() as mp:
            calls = []

            def _fake_full_auth(cfg, **kw):
                calls.append(cfg)
                return renewed

            mp.setattr("schwab_cli.auth_flows.perform_full_auth", _fake_full_auth)

            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "renewed"
        assert len(calls) == 1

    def test_tick_action_renewed_when_ttl_below_window(self, monkeypatch):
        """ttl < window → renewal path fires."""
        fresh = _session_with_refresh_ttl(DEFAULT_INTERVAL_S - 1)
        renewed = _session_with_refresh_ttl(DEFAULT_INTERVAL_S * 2)

        monkeypatch.setattr("schwab_cli.session.load", lambda: fresh)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda cfg, **kw: renewed,
            )
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "renewed"

    def test_tick_detail_is_non_empty_string(self, monkeypatch):
        fresh = _session_with_refresh_ttl(0)
        renewed = _session_with_refresh_ttl(DEFAULT_INTERVAL_S * 2)

        monkeypatch.setattr("schwab_cli.session.load", lambda: fresh)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda cfg, **kw: renewed,
            )
            tick = run_once(_CFG, now=lambda: _NOW)

        assert isinstance(tick.detail, str)
        assert tick.detail  # not empty


class TestRunOnceTokenEnsurePath:
    """ttl > DEFAULT_INTERVAL_S  →  get_session is called, NOT perform_full_auth."""

    def test_action_is_token_ensured(self, monkeypatch):
        fresh = _session_with_refresh_ttl(DEFAULT_INTERVAL_S + 1)

        monkeypatch.setattr("schwab_cli.session.load", lambda: fresh)
        with pytest.MonkeyPatch.context() as mp:
            full_auth_calls = []
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: full_auth_calls.append(True),
            )
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: fresh,
            )
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "token_ensured"
        assert full_auth_calls == [], "perform_full_auth must NOT be called on normal TTL"

    def test_perform_full_auth_not_called_on_high_ttl(self, monkeypatch):
        """Explicit guard: full-auth must stay silent when TTL is large."""
        fresh = _session_with_refresh_ttl(DEFAULT_INTERVAL_S * 10)

        monkeypatch.setattr("schwab_cli.session.load", lambda: fresh)
        with pytest.MonkeyPatch.context() as mp:
            sentinel = []
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: sentinel.append(True),
            )
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: fresh,
            )
            run_once(_CFG, now=lambda: _NOW)

        assert sentinel == []


class TestRunOnceFailurePaths:
    """Error cases must NOT raise — run_once returns a non-fatal tick."""

    def test_renew_failed_on_full_auth_exception(self, monkeypatch):
        """perform_full_auth raising any Exception → action == 'renew_failed', no escape."""
        expiring = _session_with_refresh_ttl(0)

        monkeypatch.setattr("schwab_cli.session.load", lambda: expiring)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("webauto exploded")),
            )
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "renew_failed"
        assert isinstance(tick.detail, str)

    def test_renew_failed_does_not_propagate_exception(self, monkeypatch):
        expiring = _session_with_refresh_ttl(0)

        monkeypatch.setattr("schwab_cli.session.load", lambda: expiring)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: (_ for _ in ()).throw(Exception("any error")),
            )
            # Must not raise:
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "renew_failed"

    def test_token_failed_on_session_expired(self, monkeypatch):
        """get_session raising SessionExpired → action == 'token_failed', no escape."""
        from schwab_cli.api.client import SessionExpired

        good_ttl = _session_with_refresh_ttl(DEFAULT_INTERVAL_S + 1)

        monkeypatch.setattr("schwab_cli.session.load", lambda: good_ttl)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: (_ for _ in ()).throw(SessionExpired("expired")),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: None,  # must not be called
            )
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "token_failed"
        assert isinstance(tick.detail, str)

    def test_token_failed_does_not_propagate_session_expired(self, monkeypatch):
        from schwab_cli.api.client import SessionExpired

        good_ttl = _session_with_refresh_ttl(DEFAULT_INTERVAL_S + 1)

        monkeypatch.setattr("schwab_cli.session.load", lambda: good_ttl)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: (_ for _ in ()).throw(SessionExpired("dead")),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: None,
            )
            # Must not raise:
            tick = run_once(_CFG, now=lambda: _NOW)

        assert tick.action == "token_failed"


class TestMaintenanceTickDataclass:
    """Structural contract for MaintenanceTick."""

    def test_is_frozen_dataclass(self):
        tick = MaintenanceTick(action="renewed", detail="ok")
        assert tick.action == "renewed"
        assert tick.detail == "ok"
        with pytest.raises((AttributeError, TypeError)):
            tick.action = "mutated"  # type: ignore[misc]

    def test_valid_action_strings(self):
        for action in ("renewed", "token_ensured", "renew_failed", "token_failed"):
            t = MaintenanceTick(action=action, detail="x")
            assert t.action == action


class TestDefaultInterval:
    def test_default_interval_is_8_hours(self):
        assert DEFAULT_INTERVAL_S == 8 * 3600


# ---------------------------------------------------------------------------
# Tests for run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    """run_loop orchestration: iterations, sleep calls, stop flag."""

    def _good_session(self):
        return _session_with_refresh_ttl(DEFAULT_INTERVAL_S * 10)

    def test_runs_exactly_max_iterations(self, monkeypatch):
        """With max_iterations=3 and stop=lambda:False, run_once fires 3 times."""
        monkeypatch.setattr("schwab_cli.session.load", self._good_session)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: self._good_session(),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: self._good_session(),
            )

            run_once_calls = []
            # Wrap run_once to count calls (via patching the module reference
            # that run_loop uses internally).
            original_run_once = run_once

            def _counting_run_once(cfg, **kw):
                t = original_run_once(cfg, **kw)
                run_once_calls.append(t)
                return t

            mp.setattr(
                "schwab_cli.server.maintenance.run_once",
                _counting_run_once,
            )

            sleep_calls = []
            run_loop(
                _CFG,
                interval_s=DEFAULT_INTERVAL_S,
                sleep=lambda s: sleep_calls.append(s),
                now=lambda: _NOW,
                stop=lambda: False,
                max_iterations=3,
            )

        assert len(run_once_calls) == 3

    def test_sleeps_interval_s_between_iterations(self, monkeypatch):
        """sleep is called once per iteration with interval_s."""
        monkeypatch.setattr("schwab_cli.session.load", self._good_session)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: self._good_session(),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: self._good_session(),
            )

            sleep_calls = []
            run_loop(
                _CFG,
                interval_s=DEFAULT_INTERVAL_S,
                sleep=lambda s: sleep_calls.append(s),
                now=lambda: _NOW,
                stop=lambda: False,
                max_iterations=3,
            )

        assert len(sleep_calls) == 3
        assert all(s == DEFAULT_INTERVAL_S for s in sleep_calls)

    def test_stop_flag_stops_loop_early(self, monkeypatch):
        """When stop() returns True after the first cycle, only 1 iteration runs."""
        monkeypatch.setattr("schwab_cli.session.load", self._good_session)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: self._good_session(),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: self._good_session(),
            )

            iteration_count = 0
            original_run_once = run_once

            def _counting_run_once(cfg, **kw):
                nonlocal iteration_count
                iteration_count += 1
                return original_run_once(cfg, **kw)

            mp.setattr(
                "schwab_cli.server.maintenance.run_once",
                _counting_run_once,
            )

            call_number = [0]

            def _stop_after_first():
                call_number[0] += 1
                return call_number[0] > 1

            run_loop(
                _CFG,
                interval_s=DEFAULT_INTERVAL_S,
                sleep=lambda s: None,
                now=lambda: _NOW,
                stop=_stop_after_first,
                max_iterations=10,  # would run 10 without stop
            )

        assert iteration_count == 1

    def test_run_loop_returns_none(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.session.load", self._good_session)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: self._good_session(),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: self._good_session(),
            )

            result = run_loop(
                _CFG,
                interval_s=DEFAULT_INTERVAL_S,
                sleep=lambda s: None,
                now=lambda: _NOW,
                stop=lambda: False,
                max_iterations=1,
            )

        assert result is None

    def test_custom_interval_passed_to_sleep(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.session.load", self._good_session)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: self._good_session(),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: self._good_session(),
            )

            custom_interval = 1234
            sleep_calls = []
            run_loop(
                _CFG,
                interval_s=custom_interval,
                sleep=lambda s: sleep_calls.append(s),
                now=lambda: _NOW,
                stop=lambda: False,
                max_iterations=2,
            )

        assert sleep_calls == [custom_interval, custom_interval]


# ---------------------------------------------------------------------------
# Notifier integration (injected notifier receives events)
# ---------------------------------------------------------------------------


class TestRunOnceNotifier:
    """When a notifier is injected, it should be called with each tick."""

    def test_notifier_called_on_success_tick(self, monkeypatch):
        good = _session_with_refresh_ttl(DEFAULT_INTERVAL_S + 1)

        monkeypatch.setattr("schwab_cli.session.load", lambda: good)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: good,
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: good,
            )

            notified = []
            run_once(
                _CFG,
                now=lambda: _NOW,
                notifier=lambda tick: notified.append(tick),
            )

        assert len(notified) == 1
        assert isinstance(notified[0], MaintenanceTick)

    def test_notifier_called_on_failure_tick(self, monkeypatch):
        from schwab_cli.api.client import SessionExpired

        good = _session_with_refresh_ttl(DEFAULT_INTERVAL_S + 1)
        monkeypatch.setattr("schwab_cli.session.load", lambda: good)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "schwab_cli.service.auth.get_session",
                lambda cfg: (_ for _ in ()).throw(SessionExpired("dead")),
            )
            mp.setattr(
                "schwab_cli.auth_flows.perform_full_auth",
                lambda *a, **k: good,
            )

            notified = []
            run_once(
                _CFG,
                now=lambda: _NOW,
                notifier=lambda tick: notified.append(tick),
            )

        assert len(notified) == 1
        assert notified[0].action == "token_failed"
