"""Default job configs shipped with the server-jobs feature.

``DEFAULT_JOB_CONFIGS`` maps each job stem to a full, schema-valid config dict
(the same shape ``jobs/<stem>.json`` files take). :func:`write_default_jobs`
seeds those files into a jobs directory, never overwriting an existing file.

Each default config passes :func:`schwab_cli.server.jobs.config.parse_job`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_TIMEZONE = "America/New_York"


def _command_config(
    name: str, cron: str, command: list[str], *, enabled: bool = True
) -> dict:
    """Build a full schema_version=1 command-job config dict."""
    return {
        "schema_version": 1,
        "name": name,
        "enabled": enabled,
        "cron": cron,
        "timezone": _TIMEZONE,
        "type": "command",
        "command": command,
    }


DEFAULT_JOB_CONFIGS: dict[str, dict] = {
    "market-data": _command_config(
        "Market Data (Volatility)",
        "0 17 * * 1-5",
        ["dataset", "update", "--group", "volatility", "--skip-wait"],
    ),
    "accounts": _command_config(
        "Accounts NAV Snapshot",
        "0 17 * * 1-5",
        ["dataset", "accounts", "snapshot", "--skip-wait"],
    ),
    "indices": _command_config(
        "Market Data (Indices)",
        "0 18 * * *",
        ["dataset", "update", "--indices", "--max-age-days", "6", "--skip-wait"],
    ),
    # Runs after market-data so vol_snapshots / OHLCV are fresh. Also records
    # point-in-time index membership and settles/backfills the paper ledger.
    # Off by default: a brand-new ~600-symbol daily chain fetch. Enable once
    # the earnings feed is populated (else every name fail-closes to empty).
    "screener": _command_config(
        "Options VRP Screener",
        "10 17 * * 1-5",
        ["screener", "update"],
        enabled=False,
    ),
    # Off by default: a ~600-symbol free-source sweep. Enable once the
    # earnings feed is validated; until then the screener fail-closes names
    # with an unknown earnings date (set thresholds.screener.require_earnings_date
    # = false in dataset.json to see candidates before the feed is trusted).
    "screener-earnings": _command_config(
        "Options VRP Screener — Earnings Calendar",
        "30 15 * * 1-5",
        ["screener", "earnings"],
        enabled=False,
    ),
}


def write_default_jobs(jobs_directory: Path) -> dict[str, str]:
    """Seed the default job files into ``jobs_directory``.

    Creates the directory (and parents) if missing. For each default stem,
    writes ``<stem>.json`` atomically (temp file + :func:`os.replace`) only when
    it does not already exist — an existing file is never overwritten.

    Returns ``{stem: "created" | "exists"}`` for every default stem.
    """
    jobs_directory.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}
    for stem, cfg in DEFAULT_JOB_CONFIGS.items():
        dest = jobs_directory / f"{stem}.json"
        if dest.exists():
            results[stem] = "exists"
            continue
        tmp = jobs_directory / f".{stem}.json.tmp"
        try:
            tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        results[stem] = "created"

    return results
