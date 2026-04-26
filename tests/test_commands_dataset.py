"""dataset CLI subcommands.

Uses typer.testing.CliRunner to drive the registered typer app and
capture stdout/exit codes. SQLite state is per-tmp_path via the
SCHWAB_CLI_STORAGE env var.
"""
from __future__ import annotations

import json
import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return CliRunner()


def test_dataset_help_lists_subcommands(runner):
    result = runner.invoke(app, ["dataset", "--help"])
    assert result.exit_code == 0
    for sub in ("subscribe", "unsubscribe", "status", "update", "cron"):
        assert sub in result.stdout


def test_subscribe_equity_writes_row(runner, tmp_path):
    result = runner.invoke(app, [
        "dataset", "subscribe", "NVDA,AMZN", "--group", "volatility"
    ])
    assert result.exit_code == 0
    assert "subscribed" in result.stdout.lower()
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import list_active_subscriptions
    with vol_history.connect() as conn:
        rows = list_active_subscriptions(conn, group_name="volatility")
    assert {r["symbol"] for r in rows} == {"NVDA", "AMZN"}


def test_subscribe_indices_inserts_index_subscription(runner):
    result = runner.invoke(app, [
        "dataset", "subscribe", "SPX", "--indices",
    ])
    assert result.exit_code == 0
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import list_active_index_subscriptions
    with vol_history.connect() as conn:
        rows = list_active_index_subscriptions(conn, group_name="volatility")
    assert [r["index_name"] for r in rows] == ["SPX"]


def test_subscribe_indices_rejects_unknown(runner):
    result = runner.invoke(app, [
        "dataset", "subscribe", "EFA", "--indices",
    ])
    assert result.exit_code != 0
    assert "not in supported index set" in result.stdout


def test_unsubscribe_soft_deletes(runner):
    runner.invoke(app, ["dataset", "subscribe", "NVDA"])
    result = runner.invoke(app, ["dataset", "unsubscribe", "NVDA"])
    assert result.exit_code == 0
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import list_active_subscriptions
    with vol_history.connect() as conn:
        rows = list_active_subscriptions(conn, group_name="volatility")
    assert rows == []
