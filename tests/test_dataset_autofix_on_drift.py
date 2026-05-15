"""When the cron fires past NY 17:00 ET, drift detection now both
alerts AND auto-fixes the plist."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.commands.dataset import _check_fire_time_and_alert


_NY = ZoneInfo("America/New_York")


def test_drift_triggers_autofix_and_emits_both_events(monkeypatch):
    notifier = MagicMock()
    monkeypatch.setattr(
        "schwab_cli.commands.dataset._now_ny",
        lambda: datetime(2026, 5, 15, 19, 0, tzinfo=_NY),
    )
    with patch(
        "schwab_cli.dataset.launchd.reinstall_market_data_job",
    ) as m_reinstall:
        ok = _check_fire_time_and_alert(notifier)

    assert ok is False
    m_reinstall.assert_called_once()
    events = [c.args[0] for c in notifier.emit.call_args_list]
    assert "dataset.market_data.fire_time_drift" in events
    assert "dataset.market_data.fire_time_autofixed" in events


def test_drift_autofix_failure_emits_failed_event(monkeypatch):
    """An exception in the auto-fix path is caught + reported, never
    silent."""
    notifier = MagicMock()
    monkeypatch.setattr(
        "schwab_cli.commands.dataset._now_ny",
        lambda: datetime(2026, 5, 15, 19, 0, tzinfo=_NY),
    )
    with patch(
        "schwab_cli.dataset.launchd.reinstall_market_data_job",
        side_effect=RuntimeError("launchctl bootstrap exit 5"),
    ):
        ok = _check_fire_time_and_alert(notifier)

    assert ok is False
    events = [c.args[0] for c in notifier.emit.call_args_list]
    assert "dataset.market_data.fire_time_drift" in events
    assert "dataset.market_data.fire_time_autofix_failed" in events
    # Find the failed event and verify the error payload mentions the cause.
    failed = [c for c in notifier.emit.call_args_list
              if c.args[0] == "dataset.market_data.fire_time_autofix_failed"][0]
    assert "launchctl bootstrap" in failed.kwargs["error"]


def test_safe_window_does_not_invoke_autofix(monkeypatch):
    notifier = MagicMock()
    monkeypatch.setattr(
        "schwab_cli.commands.dataset._now_ny",
        lambda: datetime(2026, 5, 15, 4, 0, tzinfo=_NY),
    )
    with patch(
        "schwab_cli.dataset.launchd.reinstall_market_data_job",
    ) as m_reinstall:
        ok = _check_fire_time_and_alert(notifier)

    assert ok is True
    m_reinstall.assert_not_called()
    assert notifier.emit.call_count == 0
