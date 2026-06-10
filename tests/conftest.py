"""Test-session fixtures shared across the whole suite.

The audit-log autouse here is the important one: it redirects every
test's audit writes into a per-session tmp directory so the
production ``~/.config/schwab_cli/scheduler.log`` is never touched
when running the suite. Without this, tests that exercise
``run_daily_sync`` end up appending fake ``pid=9999`` lines to the
operator's real log.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_daemon_delegate(monkeypatch):
    """Point token-refresh delegation at a dead port for every test.

    A REAL daemon often runs on this dev machine at 127.0.0.1:7234; an
    unmocked ``auth_delegate.request_refresh`` must fail fast against a
    closed port instead of silently triggering a live token exchange.
    Tests that exercise the delegate mock httpx (respx) or set their own
    ``SCHWAB_DAEMON_URL``.
    """
    from schwab_cli import auth_delegate

    monkeypatch.setenv("SCHWAB_DAEMON_URL", "http://127.0.0.1:9")
    auth_delegate.set_local_refresher(None)
    yield
    auth_delegate.set_local_refresher(None)


@pytest.fixture(autouse=True)
def _isolated_audit_log(monkeypatch, tmp_path):
    """Redirect ``audit_log_path`` per-test and reset the handler
    flag so each test gets a fresh attach against the tmp location."""
    from schwab_cli.dataset import audit_log

    audit_log.reset_for_tests()
    monkeypatch.setattr(
        audit_log, "audit_log_path",
        lambda: tmp_path / "scheduler.log",
    )
    yield
    audit_log.reset_for_tests()
