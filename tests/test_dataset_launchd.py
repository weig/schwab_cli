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
    INDICES_LABEL, MARKET_DATA_LABEL, VOLATILITY_LABEL,
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
    """Kind 'volatility' is now an alias for 'market-data' (the v4
    unified daily job). Label resolves to MARKET_DATA_LABEL but the
    invoked CLI still uses --group volatility for back-compat."""
    spec = DatasetPlistSpec(
        binary_path="/x/schwab_cli",
        cron="0 22 * * *",
        kind="volatility",
    )
    blob = build_dataset_plist(spec)
    parsed = plistlib.loads(blob)
    assert parsed["Label"] == MARKET_DATA_LABEL
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
    assert launcher.name == "Schwab Market Data"
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


def test_launcher_prepends_binary_dir_to_path(monkeypatch, tmp_path):
    """Launchd fires children with a minimal PATH that excludes
    ``~/.local/bin``. The launcher must prepend the binary's
    directory so child subprocess.Popen(``schwab …``) calls in the
    scheduler's pspawn loop find the binary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset.launchd import _write_launcher

    spec = DatasetPlistSpec(
        binary_path="/Users/me/.local/bin/schwab",
        cron="0 4 * * *",
        kind="scheduler",
    )
    launcher = _write_launcher(spec)
    body = launcher.read_text()
    # PATH is prepended with the binary's directory before the exec.
    assert "PATH=/Users/me/.local/bin:$PATH" in body
    assert "export PATH" in body
    assert body.index("PATH=") < body.index("exec ")


def test_unsupported_kind_rejected():
    with pytest.raises(ValueError, match="unsupported plist kind"):
        DatasetPlistSpec(binary_path="/x", cron="0 22 * * *",
                        kind="other")


# ---- install_plist: idempotent unload-before-load ---------------------


def _install_spec():
    # ``plist_path`` is derived from ``Path.home()``, so callers monkeypatch
    # HOME → tmp_path before constructing the spec to keep filesystem writes
    # inside the test sandbox.
    return DatasetPlistSpec(
        binary_path="/x/schwab_cli", cron="0 9 * * *", kind="volatility",
    )


def test_install_plist_unloads_then_loads(monkeypatch, tmp_path):
    """Reinstall must unload first — otherwise launchctl silently
    skips loading the new plist when one is already loaded at the
    same label."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset import launchd as ds_launchd

    calls: list[list[str]] = []

    class FakeResult:
        def __init__(self, returncode=0, stderr=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return FakeResult(returncode=0, stderr="")

    monkeypatch.setattr(ds_launchd.subprocess, "run", fake_run)

    ds_launchd.install_plist(_install_spec())

    assert calls[0][:2] == ["launchctl", "unload"]
    assert calls[1][:2] == ["launchctl", "load"]
    assert "-w" in calls[1]


def test_install_plist_raises_when_load_fails_silently(monkeypatch, tmp_path):
    """macOS launchctl returns 0 even when load fails. Catch it via
    stderr's ``Load failed`` text so the user sees the problem
    instead of a fake-success message."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset import launchd as ds_launchd

    class FakeResult:
        def __init__(self, returncode=0, stderr=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "load"]:
            return FakeResult(
                returncode=0,
                stderr=(
                    "Load failed: 5: Input/output error\n"
                    "Try running `launchctl bootstrap` as root for richer errors.\n"
                ),
            )
        return FakeResult()

    monkeypatch.setattr(ds_launchd.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="launchctl load failed"):
        ds_launchd.install_plist(_install_spec())


def test_install_plist_swallows_unload_noise(monkeypatch, tmp_path):
    """Unload-not-loaded prints noise on stderr but exits non-zero;
    install_plist must not surface that as a failure."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from schwab_cli.dataset import launchd as ds_launchd

    class FakeResult:
        def __init__(self, returncode=0, stderr=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "unload"]:
            return FakeResult(
                returncode=1,
                stderr='Could not find specified service\n',
            )
        return FakeResult()  # load succeeds

    monkeypatch.setattr(ds_launchd.subprocess, "run", fake_run)
    # Should not raise — unload errors are expected when nothing's loaded.
    path = ds_launchd.install_plist(_install_spec())
    assert path.exists()
