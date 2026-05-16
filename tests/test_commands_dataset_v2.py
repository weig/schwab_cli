"""``dataset cron install`` is the unified scheduler-only path:
no flags, always installs the scheduler, sweeps any pre-existing
Schwab plists first."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.dataset import config as ds_cfg
from schwab_cli.dataset import launchd as ds_launchd


runner = CliRunner()


def test_cron_install_installs_scheduler_with_hardcoded_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Cron expression comes from the launchd module — not config."""
    v2_cfg = {
        "version": 2,
        "cron": {"indices": True, "market_data": ["ohlcv", "volatility"]},
        "accounts": {"market_data": []},
    }
    monkeypatch.setattr(ds_cfg, "load_config_or_default", lambda: v2_cfg)
    monkeypatch.setattr(
        ds_cfg, "config_path", lambda: tmp_path / "dataset.json",
    )
    monkeypatch.setattr(ds_cfg, "save_config", lambda _cfg: None)
    monkeypatch.setattr(
        "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
        lambda: [],
    )

    captured = {}

    def _fake_install(spec):
        captured["spec"] = spec
        return tmp_path / f"{spec.label}.plist"

    monkeypatch.setattr(
        "schwab_cli.commands.dataset.install_plist", _fake_install,
    )

    result = runner.invoke(app, ["dataset", "cron", "install"])
    assert result.exit_code == 0, result.output
    assert captured["spec"].kind == "scheduler"
    assert captured["spec"].cron == ds_launchd.SCHEDULER_CRON_LOCAL


def test_cron_install_sweeps_legacy_plists_before_installing(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Any pre-existing Schwab plists are removed first — the
    scheduler is the only registered cron after install completes."""
    monkeypatch.setattr(
        ds_cfg, "load_config_or_default", lambda: {"version": 2},
    )
    monkeypatch.setattr(
        ds_cfg, "config_path", lambda: tmp_path / "dataset.json",
    )
    monkeypatch.setattr(ds_cfg, "save_config", lambda _cfg: None)

    removed_paths = [
        tmp_path / "com.schwab-cli.dataset.indices.plist",
        tmp_path / "com.schwab-cli.dataset.market-data.plist",
        tmp_path / "com.schwab-cli.dataset.accounts.plist",
    ]
    sweep_calls = []

    def _fake_sweep():
        sweep_calls.append(1)
        return removed_paths

    monkeypatch.setattr(
        "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
        _fake_sweep,
    )
    monkeypatch.setattr(
        "schwab_cli.commands.dataset.install_plist",
        lambda _spec: tmp_path / "scheduler.plist",
    )

    result = runner.invoke(app, ["dataset", "cron", "install"])
    assert result.exit_code == 0
    assert sweep_calls == [1]
    for path in removed_paths:
        assert str(path) in result.output


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
