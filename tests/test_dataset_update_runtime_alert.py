"""Cron fire-time drift detection — when NY-clock-at-fire is ≥ 17:00 ET,
emit a Telegram alert and skip sleep_until_ny (which would no-op
anyway)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app


_NY = ZoneInfo("America/New_York")
runner = CliRunner()


def _stubs():
    return (
        patch("schwab_cli.commands.dataset.run_volatility_update",
              return_value={"sampled": [], "skipped": [],
                            "transitions": [], "errors": [], "positions": {}}),
        patch("schwab_cli.api.client.SchwabClient",
              return_value=MagicMock()),
        patch("schwab_cli.config.load", return_value=MagicMock()),
        patch("schwab_cli.session.load", return_value=MagicMock()),
        patch("schwab_cli.dataset.config.load_config_or_default",
              return_value={"accounts": {"market_data": []}}),
        patch("schwab_cli.storage.vol_history.connect"),
    )


def test_fire_at_19_00_ny_emits_drift_alert():
    notifier = MagicMock()
    with patch("schwab_cli.commands.dataset._make_notifier",
               return_value=notifier), \
         patch("schwab_cli.commands.dataset._now_ny",
               return_value=datetime(2026, 5, 15, 19, 0, tzinfo=_NY)), \
         patch("schwab_cli.commands.dataset.sleep_until_ny") as m_sleep:
        for s in _stubs():
            s.start()
        try:
            result = runner.invoke(app, [
                "dataset", "update", "--group", "volatility",
            ])
        finally:
            for s in _stubs():
                s.stop()
    assert result.exit_code == 0, result.output
    drift_calls = [
        c for c in notifier.emit.call_args_list
        if c.args and c.args[0] == "dataset.market_data.fire_time_drift"
    ]
    assert len(drift_calls) == 1
    assert drift_calls[0].kwargs["target_ny_time"] == "17:00 ET"
    # Drift branch must SKIP sleep_until_ny (it would no-op anyway).
    m_sleep.assert_not_called()


def test_fire_at_04_00_ny_no_alert():
    notifier = MagicMock()
    with patch("schwab_cli.commands.dataset._make_notifier",
               return_value=notifier), \
         patch("schwab_cli.commands.dataset._now_ny",
               return_value=datetime(2026, 5, 15, 4, 0, tzinfo=_NY)), \
         patch("schwab_cli.commands.dataset.sleep_until_ny") as m_sleep:
        for s in _stubs():
            s.start()
        try:
            result = runner.invoke(app, [
                "dataset", "update", "--group", "volatility",
            ])
        finally:
            for s in _stubs():
                s.stop()
    assert result.exit_code == 0, result.output
    drift_calls = [
        c for c in notifier.emit.call_args_list
        if c.args and c.args[0] == "dataset.market_data.fire_time_drift"
    ]
    assert drift_calls == []
    # No drift → sleep_until_ny is still called for the wait.
    m_sleep.assert_called_once_with(17, 0)
