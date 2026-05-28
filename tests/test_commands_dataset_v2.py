"""``dataset cron`` legacy behavior.

NOTE: `dataset cron install` was retired in the server-jobs cutover — it is now a
deprecated no-op (does NOT install the scheduler plist); scheduling lives in
`schwab server` jobs (`schwab jobs migrate`). The former install tests were
removed; the new no-op contract is covered by tests/test_dataset_cron_deprecated.py.
`cron uninstall` remains a functional legacy-teardown and is still tested below.
"""
from __future__ import annotations

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.dataset import launchd as ds_launchd


runner = CliRunner()


def test_cron_uninstall_just_sweeps(monkeypatch, tmp_path):
    """`cron uninstall` is the sweep — no per-kind flags. Whatever's
    on disk gets removed; idempotent when there's nothing to remove."""
    monkeypatch.setattr(
        "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
        lambda: [],
    )
    result = runner.invoke(app, ["dataset", "cron", "uninstall"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output


def test_scheduler_cron_constant_is_a_valid_crontab():
    assert isinstance(ds_launchd.SCHEDULER_CRON_LOCAL, str)
    assert len(ds_launchd.SCHEDULER_CRON_LOCAL.split()) == 5
