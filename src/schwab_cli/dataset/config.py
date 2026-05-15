"""dataset.json — subscription declarations + thresholds + provider preferences.

Lives next to config.json. If absent, an in-memory copy of
:data:`DEFAULT_CONFIG` is returned by :func:`load_config_or_default`,
so existing CLI flows work without ever creating the file. The file
is materialized on first ``dataset cron install``.

Schema v2 (current):
- ``cron.indices``: bool — install the weekly indices job?
- ``cron.market_data``: list[str] — products inside the daily
  market-data job (e.g. ``["ohlcv", "volatility"]``). Order is fetch
  order; ohlcv must precede volatility since vol depends on cached
  closes.
- ``accounts.market_data``: list[str] — account-hash suffixes whose
  positions roll into the market_data subscription set.

Cron expressions are NOT in the config — the installer owns them
(see :mod:`schwab_cli.dataset.launchd`). The market-data job's
actual run time is anchored to NY 17:00 ET inside the Python entry
point via :func:`sleep_until_ny`.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from schwab_cli.config import config_path as _schwab_config_path


CURRENT_SCHEMA_VERSION = 2

DEFAULT_CONFIG: dict = {
    "version": CURRENT_SCHEMA_VERSION,
    "cron": {
        "indices": True,
        "market_data": ["ohlcv", "volatility"],
    },
    "accounts": {
        "market_data": [],
    },
    "thresholds": {
        "position": {
            "watch_demote_after_calendar_days":  30,
            "frozen_demote_after_calendar_days": 90,
        },
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

    Runs an idempotent v1→v2 migration when the file is at the old
    schema. The migrated config is persisted back to disk so the next
    read is a fast pass-through.
    """
    p = config_path()
    if not p.exists():
        return deepcopy(DEFAULT_CONFIG)
    raw = json.loads(p.read_text())
    if raw.get("version") == 1:
        raw = _migrate_v1_to_v2(raw)
        save_config(raw)
    return raw


def save_config(cfg: dict) -> None:
    """Write ``cfg`` atomically — ``.tmp`` → rename."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(p)


def _migrate_v1_to_v2(cfg: dict) -> dict:
    """Transform a v1 config into v2.

    v1 → v2 changes:
    * ``cron.indices`` was a cron expression; becomes ``True`` (the
      installer picks the time).
    * ``cron.groups.volatility`` (cron expression) becomes a member
      of ``cron.market_data: ["ohlcv", "volatility"]``. ``"ohlcv"``
      is auto-added — vol implies OHLCV because the vol cron needs
      underlying closes. Mirrors the DB-level v3→v4 migration.
    * ``accounts.volatility`` → ``accounts.market_data``.
    * Thresholds + indices_provider pass through unchanged.
    """
    v1_cron   = cfg.get("cron") or {}
    v1_groups = v1_cron.get("groups") or {}
    has_indices    = "indices" in v1_cron
    has_volatility = "volatility" in v1_groups

    out: dict = {
        "version": 2,
        "cron": {
            "indices": has_indices,
            "market_data": (
                ["ohlcv", "volatility"] if has_volatility else []
            ),
        },
        "accounts": {
            "market_data": (cfg.get("accounts") or {}).get("volatility") or [],
        },
    }
    for k in ("thresholds", "indices_provider"):
        if k in cfg:
            out[k] = cfg[k]
    return out
