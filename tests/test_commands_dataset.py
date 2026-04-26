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
