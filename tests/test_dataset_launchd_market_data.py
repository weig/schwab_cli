"""Plist semantics: hardcoded cron constant in code (not config),
RunAtLoad on the scheduler plist so missed runs catch up on next
boot, and the per-kind metadata registry exposes the right defaults."""
from __future__ import annotations

import plistlib

from schwab_cli.dataset.launchd import (
    SCHEDULER_CRON_LOCAL,
    DatasetPlistSpec,
    _KIND_INFO,
    build_dataset_plist,
)


def test_scheduler_cron_fires_before_ny_17et():
    """The scheduler's launchd time must fire EARLIER than the NY
    17:00 ET market-close anchor under both DST modes — the child
    processes sleep_until_ny internally to the minute. 04:00 local
    (UTC+8) is safely earlier than NY 17:00 ET in both EDT and EST."""
    assert SCHEDULER_CRON_LOCAL == "0 4 * * *"


def test_scheduler_plist_has_run_at_load():
    """Without RunAtLoad, a laptop closed at the scheduled minute
    misses the day. With it, sleep_until_ny on next boot either
    fires immediately (past target) or waits forward."""
    spec = DatasetPlistSpec(
        binary_path="/x/schwab",
        cron=SCHEDULER_CRON_LOCAL,
        kind="scheduler",
    )
    parsed = plistlib.loads(build_dataset_plist(spec))
    assert parsed["RunAtLoad"] is True


def test_scheduler_plist_program_args_invokes_dataset_sync():
    spec = DatasetPlistSpec(
        binary_path="/x/schwab",
        cron=SCHEDULER_CRON_LOCAL,
        kind="scheduler",
    )
    assert spec.program_args == ["/x/schwab", "dataset", "sync"]


def test_kind_info_registry_lists_known_kinds():
    """The pluggable kind registry is the single source of truth.
    Pinning the entries means a rename or typo blows up here rather
    than in production launchctl."""
    assert "scheduler" in _KIND_INFO
    # Legacy kinds retained for cleanup paths and back-compat
    # callers that still construct DatasetPlistSpec for non-install
    # uses (e.g. ad-hoc plist inspection).
    for legacy in ("indices", "market-data", "accounts"):
        assert legacy in _KIND_INFO
