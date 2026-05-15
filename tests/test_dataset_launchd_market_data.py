"""Phase 3 plist semantics: hardcoded cron constants in code (not
config); RunAtLoad on the market-data plist only."""
from __future__ import annotations

import plistlib

from schwab_cli.dataset.launchd import (
    INDICES_CRON_LOCAL,
    MARKET_DATA_CRON_LOCAL,
    DatasetPlistSpec,
    build_dataset_plist,
)


def test_market_data_cron_is_safely_before_ny_17et():
    """UTC+8 04:00 = NY 15:00 EDT / 16:00 EST — both safely before
    the 17:00 ET wait target."""
    assert MARKET_DATA_CRON_LOCAL == "0 4 * * *"


def test_indices_cron_unchanged():
    assert INDICES_CRON_LOCAL == "0 6 * * 0"


def test_market_data_plist_has_run_at_load():
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli",
        cron=MARKET_DATA_CRON_LOCAL,
        kind="market-data",
    )
    parsed = plistlib.loads(build_dataset_plist(spec))
    assert parsed["RunAtLoad"] is True


def test_indices_plist_does_not_have_run_at_load():
    """Indices is weekly — RunAtLoad would cause a spurious re-sync
    every reload."""
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli",
        cron=INDICES_CRON_LOCAL,
        kind="indices",
    )
    parsed = plistlib.loads(build_dataset_plist(spec))
    assert parsed["RunAtLoad"] is False
