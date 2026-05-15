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
        def execute(self, sql, *_a, **_k):
            return _FakeCursor(sql)
    class _FakeCursor:
        def __init__(self, sql=""):
            self._sql = sql
        def fetchall(self):
            return []
        def fetchone(self):
            # GROUP BY / ORDER BY queries against an empty DB return no
            # rows. Aggregate queries (MAX/COUNT) return a single
            # NULL/0 row; mimic that so callers can do `[0]`.
            if "GROUP BY symbol" in self._sql or "ROW_NUMBER" in self._sql:
                return None
            return [0]
    @contextlib.contextmanager
    def _connect():
        yield _FakeConn()
    monkeypatch.setattr("schwab_cli.storage.vol_history.connect", _connect)


def test_dataset_section_shows_market_data_with_enabled_products(
    capsys, monkeypatch, tmp_path,
):
    """When the cron is loaded, the product list renders on a `group`
    sub-line so the plist filename column stays aligned with the
    indices row. Title no longer carries the bracketed product list."""
    _stub_db(monkeypatch)
    _patch_common(monkeypatch, fire_utc=None)
    plist_path = tmp_path / "com.schwab-cli.dataset.market-data.plist"
    plist_path.write_bytes(b"<plist></plist>")
    monkeypatch.setattr(doc, "_DATASET_MARKET_DATA_PLIST", plist_path)
    monkeypatch.setattr(doc, "_launchctl_loaded", lambda _label: True)

    doc._check_dataset()
    out = capsys.readouterr().out

    assert "market_data (daily)" in out
    # Product list is on its own line as `group  …` — title stays
    # plist-aligned with the indices row.
    assert "ohlcv, volatility" in out
    # Old bracketed form must NOT appear inline with the label.
    assert "(daily) [" not in out
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


def test_market_data_stat_renders_longest_per_group(
    capsys, monkeypatch, tmp_path,
):
    """The Market Data Stat block shows the longest series per
    (group, source) so operators can see cache depth at a glance."""
    import contextlib
    from datetime import datetime as _dt, timezone as _tz

    first_ms = int(
        _dt(2025, 9, 22, tzinfo=_tz.utc).timestamp() * 1000
    )

    class _Row(dict):
        def __getitem__(self, k):
            if isinstance(k, int):
                return list(self.values())[k]
            return super().__getitem__(k)

    class _Cur:
        def __init__(self, sql):
            self.sql = sql
        def fetchall(self):
            if "ROW_NUMBER" in self.sql:
                return [
                    _Row(source="observed", symbol="AMZN",
                         n=43, first_ms=first_ms),
                    _Row(source="synthetic", symbol="INTC",
                         n=148, first_ms=first_ms),
                ]
            return []
        def fetchone(self):
            if "FROM ohlcv_daily" in self.sql and "GROUP BY symbol" in self.sql:
                return _Row(symbol="A", n=77, d="2026-01-26")
            return [0]

    class _Conn:
        def execute(self, sql, *_a, **_k):
            return _Cur(sql)

    @contextlib.contextmanager
    def _connect():
        yield _Conn()

    monkeypatch.setattr("schwab_cli.storage.vol_history.connect", _connect)
    _patch_common(monkeypatch, fire_utc=None)

    doc._check_dataset()
    out = capsys.readouterr().out
    assert "Market Data Stat" in out
    assert "OHLCV (1 day)" in out
    assert "77 since 2026-01-26" in out
    assert "(A)" not in out
    assert "volatility" in out
    assert "43 since 2025-09-22" in out
    assert "(observed)" in out
    assert "AMZN" not in out
    assert "148 since 2025-09-22" in out
    assert "(synthetic)" in out
    assert "INTC" not in out
