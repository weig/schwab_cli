"""dataset.json v2 schema: mixed-type cron block, market_data array,
accounts.market_data. v1 in-place migration is idempotent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_cli.dataset import config as ds_cfg


def test_default_config_has_v2_shape() -> None:
    cfg = ds_cfg.DEFAULT_CONFIG
    assert cfg["version"] == 2
    assert cfg["cron"]["indices"] is True
    assert cfg["cron"]["market_data"] == ["ohlcv", "volatility"]
    assert "groups" not in cfg["cron"]


def test_v1_migrates_to_v2_with_market_data_array(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    v1 = {
        "version": 1,
        "cron": {
            "indices": "0 6 * * 0",
            "groups":  {"volatility": "0 22 * * *"},
        },
        "accounts": {"volatility": ["0756"]},
        "thresholds": {"position": {"watch_demote_after_calendar_days": 30}},
        "indices_provider": {"primary": "stockanalysis"},
    }
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(v1))
    monkeypatch.setattr(ds_cfg, "config_path", lambda: p)

    cfg = ds_cfg.load_config_or_default()

    assert cfg["version"] == 2
    assert cfg["cron"]["indices"] is True
    assert cfg["cron"]["market_data"] == ["ohlcv", "volatility"]
    assert cfg["accounts"]["market_data"] == ["0756"]
    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == 2


def test_v1_without_volatility_yields_empty_market_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    v1 = {
        "version": 1,
        "cron": {"indices": "0 6 * * 0", "groups": {}},
        "accounts": {"volatility": []},
    }
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(v1))
    monkeypatch.setattr(ds_cfg, "config_path", lambda: p)

    cfg = ds_cfg.load_config_or_default()

    assert cfg["cron"]["market_data"] == []
    assert cfg["accounts"]["market_data"] == []


def test_v2_load_is_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    v2 = {
        "version": 2,
        "cron": {"indices": True, "market_data": ["ohlcv", "volatility"]},
        "accounts": {"market_data": ["0756"]},
    }
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(v2))
    monkeypatch.setattr(ds_cfg, "config_path", lambda: p)

    cfg = ds_cfg.load_config_or_default()

    assert cfg == v2
