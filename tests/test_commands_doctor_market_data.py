"""Doctor: rename labels + bracketed product list + drift warning."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.commands import doctor as doc


_NY = ZoneInfo("America/New_York")


def _patch_common(monkeypatch, fire_utc):
    """Stub out the launchd / DB-touching helpers so doc._check_dataset()
    just exercises rendering + the drift check."""
    monkeypatch.setattr(
        "schwab_cli.commands.doctor.ds_cfg.load_config_or_default"
        if hasattr(doc, "ds_cfg") else
        "schwab_cli.dataset.config.load_config_or_default",
        lambda: {
            "version": 2,
            "cron": {"indices": True,
                     "market_data": ["ohlcv", "volatility"]},
            "accounts": {"market_data": []},
        },
    )
    monkeypatch.setattr(
        doc, "_next_calendar_interval_run", lambda *_a, **_k: fire_utc,
    )
    monkeypatch.setattr(doc, "_last_market_data_run_at",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(doc, "_last_indices_run_at",
                        lambda *_a, **_k: None)


def _stub_db(monkeypatch):
    """Stub vol_history.connect() with a context manager returning a
    fake conn whose execute().fetchall()/fetchone() return safe defaults."""
    import contextlib
    class _FakeConn:
        def execute(self, *_a, **_k):
            return _FakeCursor()
    class _FakeCursor:
        def fetchall(self):
            return []
        def fetchone(self):
            return [0]
    @contextlib.contextmanager
    def _connect():
        yield _FakeConn()
    monkeypatch.setattr("schwab_cli.storage.vol_history.connect", _connect)


def test_dataset_section_shows_market_data_with_enabled_products(
    capsys, monkeypatch, tmp_path,
):
    _stub_db(monkeypatch)
    _patch_common(monkeypatch, fire_utc=None)
    # Ensure the plist path check below doesn't trip on real disk.
    monkeypatch.setattr(
        doc, "_DATASET_MARKET_DATA_PLIST", tmp_path / "nonexistent.plist",
    )

    doc._check_dataset()
    out = capsys.readouterr().out

    assert "market_data" in out
    assert "[ohlcv, volatility]" in out
    assert "volatility (daily)" not in out


def test_doctor_warns_when_fire_time_falls_after_ny_17_00(
    capsys, monkeypatch, tmp_path,
):
    """Plist fires too late → sleep_until_ny would no-op → contract
    broken. Doctor must call this out."""
    plist_path = tmp_path / "com.schwab-cli.dataset.market-data.plist"
    plist_path.write_bytes(b"<plist></plist>")  # presence is what matters

    fire_ny = datetime(2026, 5, 15, 19, 0, tzinfo=_NY)
    _stub_db(monkeypatch)
    _patch_common(monkeypatch, fire_utc=fire_ny.astimezone(timezone.utc))
    monkeypatch.setattr(doc, "_DATASET_MARKET_DATA_PLIST", plist_path)

    doc._check_dataset()
    out = capsys.readouterr().out

    assert "WARNING" in out
    assert "17:00 ET" in out
    assert "dataset cron install" in out


def test_doctor_silent_when_fire_time_safely_before_17_00(
    capsys, monkeypatch, tmp_path,
):
    plist_path = tmp_path / "com.schwab-cli.dataset.market-data.plist"
    plist_path.write_bytes(b"<plist></plist>")

    fire_ny = datetime(2026, 5, 15, 4, 0, tzinfo=_NY)
    _stub_db(monkeypatch)
    _patch_common(monkeypatch, fire_utc=fire_ny.astimezone(timezone.utc))
    monkeypatch.setattr(doc, "_DATASET_MARKET_DATA_PLIST", plist_path)

    doc._check_dataset()
    out = capsys.readouterr().out
    assert "WARNING" not in out
