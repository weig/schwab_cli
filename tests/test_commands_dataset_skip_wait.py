"""--skip-wait bypasses sleep_until_ny for manual reruns; the default
path waits."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from schwab_cli.cli import app


_NY = ZoneInfo("America/New_York")
runner = CliRunner()


def _stubs_for_invoke():
    """Mocks for every lazy-imported dep inside update_cmd so the
    cron path runs without touching disk / network."""
    return (
        patch("schwab_cli.commands.dataset.run_volatility_update",
              return_value={"sampled": [], "skipped": [],
                            "transitions": [], "errors": [], "positions": {}}),
        patch("schwab_cli.api.client.SchwabClient",
              return_value=MagicMock()),
        patch("schwab_cli.config.load",
              return_value=MagicMock()),
        patch("schwab_cli.session.load",
              return_value=MagicMock()),
        patch("schwab_cli.dataset.config.load_config_or_default",
              return_value={"accounts": {"market_data": []}}),
        patch("schwab_cli.storage.vol_history.connect"),
    )


def test_skip_wait_bypasses_sleep_until_ny():
    stubs = _stubs_for_invoke()
    with patch("schwab_cli.commands.dataset.sleep_until_ny") as m_sleep:
        for s in stubs:
            s.start()
        try:
            result = runner.invoke(app, [
                "dataset", "update", "--group", "volatility", "--skip-wait",
            ])
        finally:
            for s in stubs:
                s.stop()
    assert result.exit_code == 0, result.output
    m_sleep.assert_not_called()


def test_default_path_calls_sleep_until_ny():
    """Default invocation in the safe NY window (< 17:00 ET) waits."""
    stubs = _stubs_for_invoke()
    with patch("schwab_cli.commands.dataset.sleep_until_ny") as m_sleep, \
         patch("schwab_cli.commands.dataset._now_ny",
               return_value=datetime(2026, 5, 15, 4, 0, tzinfo=_NY)):
        for s in stubs:
            s.start()
        try:
            result = runner.invoke(app, [
                "dataset", "update", "--group", "volatility",
            ])
        finally:
            for s in stubs:
                s.stop()
    assert result.exit_code == 0, result.output
    m_sleep.assert_called_once_with(17, 0)
