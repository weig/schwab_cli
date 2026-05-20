"""Direct tests for ``uninstall_all_schwab_plists`` and the
``_unload_or_raise`` helper. The CLI tests mock the sweep entirely;
these pin its actual contract."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from schwab_cli.dataset import launchd as ld


# ---- _unload_or_raise -------------------------------------------------


def _fake_run_factory(rc: int, stderr: str):
    """Stub ``subprocess.run`` returning a fixed (rc, stderr)."""
    def _run(*_args, **_kwargs):
        return SimpleNamespace(returncode=rc, stderr=stderr, stdout="")
    return _run


def test_unload_silent_on_zero_exit_with_empty_stderr(monkeypatch):
    monkeypatch.setattr(ld.subprocess, "run", _fake_run_factory(0, ""))
    ld._unload_or_raise(Path("/tmp/x.plist"))  # must not raise


def test_unload_silent_on_not_loaded_hint(monkeypatch):
    """macOS variants exit non-zero with this stderr when the
    service was never loaded. Treat as success."""
    monkeypatch.setattr(
        ld.subprocess, "run",
        _fake_run_factory(3, "Could not find specified service"),
    )
    ld._unload_or_raise(Path("/tmp/x.plist"))  # must not raise


def test_unload_raises_on_real_failure_even_when_exit_zero(monkeypatch):
    """The bug we're guarding against: launchctl exits 0 but stderr
    carries a real diagnostic (SIP, sandbox, corrupt plist). The old
    rule "exit 0 = success" silently deleted the on-disk plist while
    launchd still had the job registered."""
    monkeypatch.setattr(
        ld.subprocess, "run",
        _fake_run_factory(0, "Operation not permitted"),
    )
    with pytest.raises(RuntimeError, match="operation not permitted"):
        ld._unload_or_raise(Path("/tmp/x.plist"))


def test_unload_raises_on_nonzero_with_arbitrary_stderr(monkeypatch):
    monkeypatch.setattr(
        ld.subprocess, "run",
        _fake_run_factory(5, "Load failed: 5: Input/output error"),
    )
    with pytest.raises(RuntimeError, match="load failed"):
        ld._unload_or_raise(Path("/tmp/x.plist"))


# ---- uninstall_all_schwab_plists --------------------------------------


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Make subprocess.run a no-op so we don't try to talk to launchctl.
    monkeypatch.setattr(ld.subprocess, "run", _fake_run_factory(0, ""))
    return tmp_path


def _write_plist(home: Path, name: str) -> Path:
    plist = home / "Library" / "LaunchAgents" / name
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"<plist></plist>")
    return plist


def test_sweep_removes_scheduler_plist(fake_home):
    """The scheduler plist is the only one we own."""
    p1 = _write_plist(fake_home, "com.schwab-cli.scheduler.plist")

    removed = ld.uninstall_all_schwab_plists()

    assert removed == [p1]
    assert not p1.exists()


def test_sweep_leaves_legacy_dataset_plists_alone(fake_home):
    """Old per-job plists from a pre-unified-scheduler install are no
    longer ours to manage — leave them alone."""
    legacy = _write_plist(
        fake_home, "com.schwab-cli.dataset.market-data.plist",
    )
    legacy_underscore = _write_plist(
        fake_home, "com.schwab_cli.dataset.legacy.plist",
    )

    removed = ld.uninstall_all_schwab_plists()

    assert removed == []
    assert legacy.exists()
    assert legacy_underscore.exists()


def test_sweep_leaves_non_schwab_plists_alone(fake_home):
    """A stray non-Schwab plist must survive the sweep — we don't
    own those files."""
    ours = _write_plist(fake_home, "com.schwab-cli.scheduler.plist")
    theirs = _write_plist(fake_home, "com.example.someone-else.plist")

    removed = ld.uninstall_all_schwab_plists()

    assert removed == [ours]
    assert not ours.exists()
    assert theirs.exists()


def test_sweep_does_not_touch_mcp_plist(fake_home):
    """Regression: the MCP server installs itself under the same
    `com.schwab-cli.` prefix but is NOT a dataset cron job. The
    sweep must leave it alone — an earlier too-broad prefix list
    silently uninstalled the user's MCP plist."""
    scheduler_plist = _write_plist(
        fake_home, "com.schwab-cli.scheduler.plist",
    )
    mcp_plist = _write_plist(fake_home, "com.schwab-cli.mcp.plist")

    removed = ld.uninstall_all_schwab_plists()

    # MCP plist must survive.
    assert mcp_plist.exists()
    assert mcp_plist not in removed
    # Scheduler plist must be removed.
    assert not scheduler_plist.exists()
    assert removed == [scheduler_plist]


def test_sweep_returns_empty_when_launchagents_missing(monkeypatch, tmp_path):
    """A fresh system with no LaunchAgents directory must return []
    without raising."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Deliberately do NOT create ~/Library/LaunchAgents.
    monkeypatch.setattr(ld.subprocess, "run", _fake_run_factory(0, ""))
    assert ld.uninstall_all_schwab_plists() == []


def test_sweep_leaves_plist_on_disk_when_unload_raises(fake_home, monkeypatch):
    """Real-failure contract: if _unload_or_raise raises, the plist
    is NOT deleted. Caller can investigate without ending up with a
    registered launchd job and no plist to manage it."""
    p = _write_plist(fake_home, "com.schwab-cli.scheduler.plist")

    def _angry_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=5, stderr="Load failed: 5: I/O error", stdout="",
        )

    monkeypatch.setattr(ld.subprocess, "run", _angry_run)

    with pytest.raises(RuntimeError):
        ld.uninstall_all_schwab_plists()

    # Plist file MUST still be on disk.
    assert p.exists()


def test_sweep_only_removes_known_launcher_basenames(fake_home):
    """Launcher cleanup is gated by names in _KIND_INFO — anything
    else in the launcher directory survives."""
    # Pre-create the launcher directory with one of ours + one stray.
    launcher_dir = fake_home / "Library" / "Application Support" / \
        "schwab_cli" / "launchers"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    ours = launcher_dir / "Schwab Data Sync Service"
    ours.write_text("#!/bin/sh\nexit 0\n")
    theirs = launcher_dir / "Unrelated User File.txt"
    theirs.write_text("data")

    ld.uninstall_all_schwab_plists()

    assert not ours.exists()
    assert theirs.exists()
