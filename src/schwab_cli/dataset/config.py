"""dataset.json — schedule + thresholds + provider preferences.

Lives next to config.json. If absent, an in-memory copy of
:data:`DEFAULT_CONFIG` is returned by :func:`load_config_or_default`,
so existing CLI flows work without ever creating the file. The file
is materialized on first ``dataset cron install``.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from schwab_cli.config import config_path as _schwab_config_path


DEFAULT_CONFIG: dict = {
    "version": 1,
    "cron": {
        "indices": "0 6 * * 0",
        "groups":  {"volatility": "0 22 * * *"},
    },
    "thresholds": {
        "indices": {
            "active_min_chain_volume":             5000,
            "active_min_front2_oi":                10000,
            "watch_demote_after_trading_days":     7,
            "frozen_demote_after_calendar_days":   30,
        },
        "position": {
            "watch_demote_after_calendar_days":    30,
            "frozen_demote_after_calendar_days":   90,
        },
        "grace_trading_days":                      7,
    },
    "indices_provider": {
        "primary":  "stockanalysis",
        "fallback": "ssga",
    },
}


def config_path() -> Path:
    return _schwab_config_path().parent / "dataset.json"


def load_config_or_default() -> dict:
    """Return the on-disk config or a deep copy of :data:`DEFAULT_CONFIG`.

    Never raises on missing file. Raises :class:`json.JSONDecodeError`
    on malformed JSON — caller should fail fast in that case.
    """
    p = config_path()
    if not p.exists():
        return deepcopy(DEFAULT_CONFIG)
    return json.loads(p.read_text())


def save_config(cfg: dict) -> None:
    """Write ``cfg`` atomically — ``.tmp`` → rename."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(p)
