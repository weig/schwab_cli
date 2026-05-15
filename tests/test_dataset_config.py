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
    """v2 shape — installer owns cron expressions, market_data is a
    product array. See test_dataset_config_v2.py for migration tests."""
    assert DEFAULT_CONFIG["version"] == 2
    assert DEFAULT_CONFIG["cron"]["indices"] is True
    assert DEFAULT_CONFIG["cron"]["market_data"] == ["ohlcv", "volatility"]
    thr = DEFAULT_CONFIG["thresholds"]
    assert thr["position"]["watch_demote_after_calendar_days"] == 30
    assert thr["position"]["frozen_demote_after_calendar_days"] == 90


def test_load_returns_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config_or_default()
    assert cfg == DEFAULT_CONFIG


def test_save_then_load_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = json.loads(json.dumps(DEFAULT_CONFIG))
    custom["thresholds"]["position"]["watch_demote_after_calendar_days"] = 14
    save_config(custom)

    loaded = load_config_or_default()
    assert (
        loaded["thresholds"]["position"]["watch_demote_after_calendar_days"]
        == 14
    )


def test_config_path_lives_next_to_config_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = config_path()
    assert p.parent.name == "schwab_cli"
    assert p.name == "dataset.json"
