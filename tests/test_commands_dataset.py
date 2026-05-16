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


def test_status_outputs_table_for_subscribed_symbol(runner):
    runner.invoke(app, ["dataset", "subscribe", "NVDA"])
    result = runner.invoke(app, ["dataset", "status"])
    assert result.exit_code == 0
    assert "NVDA" in result.stdout
    assert "GRACE" in result.stdout


def test_status_json_output(runner):
    runner.invoke(app, ["dataset", "subscribe", "NVDA"])
    result = runner.invoke(app, ["dataset", "status", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed[0]["symbol"] == "NVDA"
    assert parsed[0]["tier"] == "GRACE"


def test_update_indices_calls_orchestrator(runner, monkeypatch):
    runner.invoke(app, ["dataset", "subscribe", "SPX", "--indices"])

    calls = []
    def fake_run(conn, *, http_client, group_name, now_ms):
        calls.append("indices")
        return {"SPX": {"added": ["AAPL"], "removed": [], "total": 1}}

    monkeypatch.setattr(
        "schwab_cli.commands.dataset.run_indices_update", fake_run
    )

    result = runner.invoke(app, ["dataset", "update", "--indices"])
    assert result.exit_code == 0
    assert calls == ["indices"]
    assert "SPX" in result.stdout


def test_update_group_volatility_calls_orchestrator(runner, monkeypatch):
    calls = []
    def fake_run(conn, *, client, group_name, now_ms, accounts,
                 progress=None):
        calls.append("vol")
        return {"sampled": ["NVDA"], "skipped": [],
                "transitions": [], "errors": [], "positions": {}}

    # Provide stub config + session so update doesn't bail early.
    monkeypatch.setattr(
        "schwab_cli.commands.dataset.run_volatility_update", fake_run
    )
    # Stub auth/session loaders.
    import schwab_cli.config as cfg_mod
    import schwab_cli.session as sess_mod
    monkeypatch.setattr(cfg_mod, "load", lambda: object())
    monkeypatch.setattr(sess_mod, "load", lambda: object())
    # Stub SchwabClient ctor — it's invoked but the fake_run ignores it.
    import schwab_cli.api.client as client_mod
    monkeypatch.setattr(client_mod, "SchwabClient", lambda c, s: object())

    result = runner.invoke(app, ["dataset", "update", "--group", "volatility"])
    assert result.exit_code == 0
    assert calls == ["vol"]


def test_update_requires_indices_or_group(runner):
    result = runner.invoke(app, ["dataset", "update"])
    assert result.exit_code != 0


def test_cron_install_writes_scheduler_plist(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
        lambda: [],
    )
    calls = []
    def fake_install(spec):
        calls.append((spec.kind, spec.cron, spec.binary_path))
        spec.plist_path.parent.mkdir(parents=True, exist_ok=True)
        spec.plist_path.write_bytes(b"<plist></plist>")
        return spec.plist_path
    monkeypatch.setattr(
        "schwab_cli.commands.dataset.install_plist", fake_install
    )

    result = runner.invoke(app, ["dataset", "cron", "install"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "scheduler"
    assert calls[0][1] == "0 4 * * *"
    plist = tmp_path / "Library" / "LaunchAgents" / \
            "com.schwab-cli.scheduler.plist"
    assert plist.exists()


def test_cron_uninstall_sweeps_all(runner, monkeypatch, tmp_path):
    """`cron uninstall` removes every Schwab plist, no per-kind flag."""
    monkeypatch.setenv("HOME", str(tmp_path))
    captured = []
    def fake_sweep():
        captured.append("swept")
        return [tmp_path / "com.schwab-cli.scheduler.plist"]
    monkeypatch.setattr(
        "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
        fake_sweep,
    )

    result = runner.invoke(app, ["dataset", "cron", "uninstall"])
    assert result.exit_code == 0
    assert captured == ["swept"]
