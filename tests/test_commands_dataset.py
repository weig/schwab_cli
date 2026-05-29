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

    # Use --skip-wait so the test doesn't sleep until NY 17:00 ET
    # when invoked outside business hours.
    result = runner.invoke(app, [
        "dataset", "update", "--group", "volatility", "--skip-wait",
    ])
    assert result.exit_code == 0
    assert calls == ["vol"]


def _run_vol_update_with_summary(runner, monkeypatch, summary):
    """Invoke `dataset update --group volatility --skip-wait` with a
    stubbed run_volatility_update returning ``summary``. Returns the
    CLI result so the caller can assert on exit_code."""
    def fake_run(conn, *, client, group_name, now_ms, accounts, progress=None):
        return summary

    monkeypatch.setattr(
        "schwab_cli.commands.dataset.run_volatility_update", fake_run
    )
    import schwab_cli.api.client as client_mod
    import schwab_cli.config as cfg_mod
    import schwab_cli.session as sess_mod
    monkeypatch.setattr(cfg_mod, "load", lambda: object())
    monkeypatch.setattr(sess_mod, "load", lambda: object())
    monkeypatch.setattr(client_mod, "SchwabClient", lambda c, s: object())
    return runner.invoke(app, [
        "dataset", "update", "--group", "volatility", "--skip-wait",
    ])


def test_update_norun_rerun_with_skips_exits_zero(runner, monkeypatch):
    """Regression: a no-op RE-RUN (everything already sampled today →
    skipped) with a few incidental errors must NOT fail. sampled=0 here
    is expected, not catastrophic. Previously this false-failed with
    exit 1 and fired a bogus scheduler.job_failed alert."""
    result = _run_vol_update_with_summary(runner, monkeypatch, {
        "sampled": [], "skipped": ["A"] * 515,
        "errors": [{"symbol": "X", "error": "no chain"}] * 3,
        "transitions": [], "positions": {},
    })
    assert result.exit_code == 0, result.output


def test_update_total_failure_nonauth_exits_one(runner, monkeypatch):
    """Nothing sampled, nothing skipped, all errors non-auth → exit 1."""
    result = _run_vol_update_with_summary(runner, monkeypatch, {
        "sampled": [], "skipped": [],
        "errors": [{"symbol": "X", "error": "boom"}] * 3,
        "transitions": [], "positions": {},
    })
    assert result.exit_code == 1


def test_update_total_failure_all_auth_exits_two(runner, monkeypatch):
    """Nothing sampled/skipped, every error is a session-expiry → exit
    EXIT_AUTH_FAILED (2) so the scheduler re-auths and retries."""
    from schwab_cli._exit_codes import EXIT_AUTH_FAILED
    result = _run_vol_update_with_summary(runner, monkeypatch, {
        "sampled": [], "skipped": [],
        "errors": [
            {"symbol": "X", "error": "Session expired. Run `schwab_cli auth --force`."}
        ] * 3,
        "transitions": [], "positions": {},
    })
    assert result.exit_code == EXIT_AUTH_FAILED


def test_update_partial_success_with_errors_exits_zero(runner, monkeypatch):
    """sampled>0 with some errors is partial success — must exit 0."""
    result = _run_vol_update_with_summary(runner, monkeypatch, {
        "sampled": ["NVDA"] * 500, "skipped": [],
        "errors": [{"symbol": "X", "error": "boom"}] * 18,
        "transitions": [], "positions": {},
    })
    assert result.exit_code == 0, result.output


def test_update_requires_indices_or_group(runner):
    result = runner.invoke(app, ["dataset", "update"])
    assert result.exit_code != 0


# `dataset cron install` was retired in the server-jobs cutover — it is now a
# deprecated no-op that does NOT install the scheduler plist. The new contract is
# covered by tests/test_dataset_cron_deprecated.py; the former
# test_cron_install_writes_scheduler_plist was removed accordingly.


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
