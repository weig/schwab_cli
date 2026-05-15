"""commands/dataset.py reads v2 config shape — cron expressions from
launchd constants, account lookups under cfg["accounts"]["market_data"]."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.dataset import config as ds_cfg
from schwab_cli.dataset import launchd as ds_launchd


runner = CliRunner()


def test_cron_install_uses_launchd_constants_not_config_cron(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Install must NOT read cron expressions from the config dict —
    they're hardcoded in the launchd module."""
    v2_cfg = {
        "version": 2,
        "cron": {"indices": True, "market_data": ["ohlcv", "volatility"]},
        "accounts": {"market_data": []},
    }
    monkeypatch.setattr(ds_cfg, "load_config_or_default", lambda: v2_cfg)
    monkeypatch.setattr(ds_cfg, "config_path",
                        lambda: tmp_path / "dataset.json")
    monkeypatch.setattr(ds_cfg, "save_config", lambda _cfg: None)

    captured = {}
    def _fake_install(spec):
        captured["spec"] = spec
        return tmp_path / f"com.schwab-cli.dataset.{spec.kind}.plist"
    monkeypatch.setattr(
        "schwab_cli.commands.dataset.install_plist", _fake_install,
    )

    result = runner.invoke(
        app, ["dataset", "cron", "install", "--group", "volatility"],
    )
    assert result.exit_code == 0
    assert captured["spec"].cron == ds_launchd.MARKET_DATA_CRON_LOCAL


def test_cron_install_indices_uses_launchd_indices_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    v2_cfg = {
        "version": 2,
        "cron": {"indices": True, "market_data": ["ohlcv", "volatility"]},
        "accounts": {"market_data": []},
    }
    monkeypatch.setattr(ds_cfg, "load_config_or_default", lambda: v2_cfg)
    monkeypatch.setattr(ds_cfg, "config_path",
                        lambda: tmp_path / "dataset.json")
    monkeypatch.setattr(ds_cfg, "save_config", lambda _cfg: None)

    captured = {}
    def _fake_install(spec):
        captured["spec"] = spec
        return tmp_path / f"com.schwab-cli.dataset.{spec.kind}.plist"
    monkeypatch.setattr(
        "schwab_cli.commands.dataset.install_plist", _fake_install,
    )

    result = runner.invoke(app, ["dataset", "cron", "install", "--indices"])
    assert result.exit_code == 0
    assert captured["spec"].cron == ds_launchd.INDICES_CRON_LOCAL


def test_launchd_has_market_data_cron_constants():
    """Constants exist and look like cron expressions."""
    assert isinstance(ds_launchd.INDICES_CRON_LOCAL, str)
    assert isinstance(ds_launchd.MARKET_DATA_CRON_LOCAL, str)
    assert len(ds_launchd.INDICES_CRON_LOCAL.split()) == 5
    assert len(ds_launchd.MARKET_DATA_CRON_LOCAL.split()) == 5
