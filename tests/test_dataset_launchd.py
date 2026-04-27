"""Crontab string → launchd StartCalendarInterval translation.

Supports the standard 5-field crontab grammar (min hour day month dow).
Rejects anything we can't translate (ranges, steps, names, @daily) so
behavior stays predictable.
"""
from __future__ import annotations

import pytest

from schwab_cli.dataset.launchd import crontab_to_calendar_interval


def test_daily_22_00():
    out = crontab_to_calendar_interval("0 22 * * *")
    assert out == [{"Hour": 22, "Minute": 0}]


def test_weekly_sunday_06_00():
    out = crontab_to_calendar_interval("0 6 * * 0")
    assert out == [{"Hour": 6, "Minute": 0, "Weekday": 0}]


def test_specific_dom():
    out = crontab_to_calendar_interval("30 9 1 * *")
    assert out == [{"Hour": 9, "Minute": 30, "Day": 1}]


def test_rejects_step():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("*/15 * * * *")


def test_rejects_range():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("0 9-17 * * *")


def test_rejects_named_shorthand():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("@daily")


def test_rejects_wrong_field_count():
    with pytest.raises(ValueError, match="5 fields"):
        crontab_to_calendar_interval("0 22 * *")


def test_field_value_out_of_range():
    with pytest.raises(ValueError, match="hour"):
        crontab_to_calendar_interval("0 25 * * *")


import plistlib

from schwab_cli.dataset.launchd import (
    build_dataset_plist, DatasetPlistSpec,
    INDICES_LABEL, VOLATILITY_LABEL,
)


def test_indices_plist_label_and_program_args():
    spec = DatasetPlistSpec(
        binary_path="/usr/local/bin/schwab_cli",
        cron="0 6 * * 0",
        kind="indices",
    )
    blob = build_dataset_plist(spec)
    parsed = plistlib.loads(blob)
    assert parsed["Label"] == INDICES_LABEL
    assert parsed["ProgramArguments"] == [
        "/usr/local/bin/schwab_cli", "dataset", "update", "--indices",
    ]
    assert parsed["StartCalendarInterval"] == [
        {"Hour": 6, "Minute": 0, "Weekday": 0}
    ]
    assert parsed["RunAtLoad"] is False
    assert parsed["KeepAlive"] is False


def test_volatility_plist_args():
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli",
        cron="0 22 * * *",
        kind="volatility",
    )
    blob = build_dataset_plist(spec)
    parsed = plistlib.loads(blob)
    assert parsed["Label"] == VOLATILITY_LABEL
    assert parsed["ProgramArguments"] == [
        "/x/schwab_cli", "dataset", "update", "--group", "volatility",
    ]


def test_log_paths_attached_when_provided():
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli",
        cron="0 22 * * *",
        kind="volatility",
        log_file="/tmp/dataset.log",
    )
    parsed = plistlib.loads(build_dataset_plist(spec))
    assert parsed["StandardOutPath"] == "/tmp/dataset.log"
    assert parsed["StandardErrorPath"] == "/tmp/dataset.log"


def test_plist_uses_launcher_basename_for_friendly_display(monkeypatch, tmp_path):
    """The plist must reference the friendly-named launcher script
    so System Settings → Login Items shows e.g. ``Schwab Indices Dataset``
    instead of the bare ``schwab_cli`` binary name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset.launchd import _write_launcher
    spec = DatasetPlistSpec(
        binary_path="/usr/local/bin/schwab_cli",
        cron="0 22 * * *",
        kind="volatility",
    )
    launcher = _write_launcher(spec)
    assert launcher.name == "Schwab Volatility Dataset"
    assert launcher.exists()
    # Launcher is executable + execs the real binary with our args.
    body = launcher.read_text()
    assert body.startswith("#!/bin/sh")
    assert "dataset update --group volatility" in body
    assert "/usr/local/bin/schwab_cli" in body

    blob = build_dataset_plist(spec, launcher_path=launcher)
    parsed = plistlib.loads(blob)
    assert parsed["ProgramArguments"] == [str(launcher)]


def test_indices_launcher_has_friendly_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset.launchd import _write_launcher
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli", cron="0 6 * * 0", kind="indices",
    )
    launcher = _write_launcher(spec)
    assert launcher.name == "Schwab Indices Dataset"
    assert "dataset update --indices" in launcher.read_text()


def test_unsupported_kind_rejected():
    with pytest.raises(ValueError, match="unsupported plist kind"):
        DatasetPlistSpec(binary_path="/x", cron="0 22 * * *",
                        kind="other")
