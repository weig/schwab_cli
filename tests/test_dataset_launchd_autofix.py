"""Phase 4 — auto-fix plist when fire-time drift is detected.

When the cron fires and notices NY-clock-at-fire is ≥ 17:00 ET (the
``sleep_until_ny`` no-op branch — design contract broken), it should
recompute the right local Hour for the current system TZ, rewrite the
plist, re-bootstrap via launchctl, and emit a follow-up Telegram
notification telling the operator what just changed.

Safety rule for the new local Hour: in either DST mode the fire must
land at NY ≤ 16:00 (1-hour buffer before the 17:00 target), so
sleep_until_ny still has room to wait. Picking the EST anchor (worst
case for "earlier than target") guarantees the EDT case is safe too.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.dataset import launchd


@pytest.mark.parametrize(
    "system_tz_name, expected_hour",
    [
        # UTC+8 Asia/Taipei: NY 16:00 EST = UTC 21:00 → 05:00 next day +8
        ("Asia/Taipei", 5),
        # UTC+0 (year-round, no DST): NY 16:00 EST = UTC 21:00 → 21:00 local
        ("Africa/Abidjan", 21),
        # NY itself: NY 16:00 EST = 16:00 local
        ("America/New_York", 16),
    ],
)
def test_compute_safe_local_hour_picks_pre_17ET_slot(
    system_tz_name, expected_hour,
):
    """Hour computed against the EST anchor (the worst case for
    "earlier than 17:00 ET" because EST is further from UTC than EDT)."""
    assert launchd._compute_safe_local_hour(
        system_tz=ZoneInfo(system_tz_name),
    ) == expected_hour


def test_reinstall_market_data_job_rewrites_plist_and_bootstraps(
    monkeypatch, tmp_path,
):
    """``reinstall_market_data_job(local_hour=H)`` writes a plist with
    StartCalendarInterval Hour=H and re-loads via launchctl
    bootout + bootstrap."""
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    plist_path = plist_dir / "com.schwab-cli.dataset.market-data.plist"
    plist_path.write_bytes(b"<plist><dict></dict></plist>")  # stub legacy

    monkeypatch.setattr(launchd, "_default_dir", lambda: plist_dir)

    calls = []
    def fake_run(cmd, **_k):
        calls.append(cmd)
        return MagicMock(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(launchd.subprocess, "run", fake_run)

    launchd.reinstall_market_data_job(local_hour=5)

    # Plist now has Hour=5
    import plistlib
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["StartCalendarInterval"] == [{"Hour": 5, "Minute": 0}]
    # bootout + bootstrap both invoked
    flat = [" ".join(c) for c in calls]
    assert any("bootout" in line for line in flat)
    assert any("bootstrap" in line for line in flat)


def test_reinstall_is_idempotent_when_hour_unchanged(monkeypatch, tmp_path):
    """No-op when the existing plist already has the correct Hour —
    avoids unnecessary launchctl churn each cron run."""
    import plistlib
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    plist_path = plist_dir / "com.schwab-cli.dataset.market-data.plist"
    plist_path.write_bytes(plistlib.dumps({
        "Label": "com.schwab-cli.dataset.market-data",
        "StartCalendarInterval": [{"Hour": 5, "Minute": 0}],
        "ProgramArguments": ["/x"],
    }))

    monkeypatch.setattr(launchd, "_default_dir", lambda: plist_dir)
    calls = []
    monkeypatch.setattr(launchd.subprocess, "run",
                        lambda cmd, **_k: calls.append(cmd) or MagicMock())

    launchd.reinstall_market_data_job(local_hour=5)

    assert calls == []  # nothing invoked — Hour matches
