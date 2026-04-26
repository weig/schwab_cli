"""dataset.json load/save with lazy defaults.

The file lives next to config.json. If missing, callers get the
default config in memory; the file is only created on first
`dataset cron install` (Task 28).
"""
from __future__ import annotations

import json

import pytest

from schwab_cli.dataset.config import (
    DEFAULT_CONFIG,
    config_path,
    load_config_or_default,
    save_config,
)


def test_default_config_shape():
    assert DEFAULT_CONFIG["version"] == 1
    assert DEFAULT_CONFIG["cron"]["indices"] == "0 6 * * 0"
    assert DEFAULT_CONFIG["cron"]["groups"]["volatility"] == "0 22 * * *"
    thr = DEFAULT_CONFIG["thresholds"]
    assert thr["indices"]["active_min_chain_volume"] == 5000
    assert thr["indices"]["active_min_front2_oi"] == 10000
    assert thr["grace_trading_days"] == 7


def test_load_returns_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config_or_default()
    assert cfg == DEFAULT_CONFIG


def test_save_then_load_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = json.loads(json.dumps(DEFAULT_CONFIG))
    custom["thresholds"]["indices"]["active_min_chain_volume"] = 12345
    save_config(custom)

    loaded = load_config_or_default()
    assert loaded["thresholds"]["indices"]["active_min_chain_volume"] == 12345


def test_config_path_lives_next_to_config_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = config_path()
    assert p.parent.name == "schwab_cli"
    assert p.name == "dataset.json"
