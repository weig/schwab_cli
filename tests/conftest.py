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
