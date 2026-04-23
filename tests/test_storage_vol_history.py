"""Tests for the vol_history SQLite store.

The `SCHWAB_CLI_STORAGE` env override keeps every test isolated in a
tmp_path — no risk of polluting the real store at
``~/.config/schwab_cli/storage/vol_history.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.storage import storage_dir
from schwab_cli.storage.vol_history import (
    connect,
    db_path,
    read_recent_per_day,
    record_snapshot,
)


NY = ZoneInfo("America/New_York")


def _ms_at(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> int:
    """Millisecond epoch for a wall-clock instant in NY."""
    ts = datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(timezone.utc).timestamp()
    return int(ts * 1000)


# ---- storage_dir resolution --------------------------------------------


def test_storage_dir_honors_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "custom"))
    assert storage_dir() == Path(str(tmp_path / "custom"))


def test_storage_dir_defaults_to_config_sibling(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHWAB_CLI_STORAGE", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert storage_dir() == tmp_path / ".config" / "schwab_cli" / "storage"


# ---- schema + connect --------------------------------------------------


def test_connect_creates_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "vol_snapshots" in tables
    assert "schema_version" in tables


def test_connect_creates_parent_dir_if_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "not-yet"))
    # Dir doesn't exist yet — connect() should create it.
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    assert db_path().exists()


def test_connect_is_reentrant(monkeypatch, tmp_path):
    """Re-opening the store against an existing DB doesn't error."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.35,
            atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=_ms_at(2026, 4, 23),
        )
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vol_snapshots").fetchone()[0]
    assert n == 1


# ---- record_snapshot ---------------------------------------------------


def test_record_snapshot_writes_row(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=202.5, atm_iv=0.3658,
            atm_strike=202.5, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=_ms_at(2026, 4, 23, 16, 0),
        )
        row = conn.execute(
            "SELECT * FROM vol_snapshots WHERE symbol='NVDA'"
        ).fetchone()
    assert row["atm_iv"] == pytest.approx(0.3658)
    assert row["spot"] == pytest.approx(202.5)
    assert row["atm_expiry"] == "2026-05-01"
    assert row["atm_dte"] == 9


def test_record_snapshot_idempotent_on_same_pk(monkeypatch, tmp_path):
    """Two writes with identical (captured_at_ms, symbol) stay as one row."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    t = _ms_at(2026, 4, 23)
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=t,
        )
        # Second write at the same ms — INSERT OR IGNORE is the lock.
        record_snapshot(
            conn, symbol="NVDA", spot=201.0, atm_iv=0.99,
            atm_strike=201.0, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=t,
        )
        rows = conn.execute("SELECT COUNT(*) FROM vol_snapshots").fetchone()[0]
        iv = conn.execute("SELECT atm_iv FROM vol_snapshots").fetchone()[0]
    assert rows == 1
    # First write wins — INSERT OR IGNORE keeps the original value.
    assert iv == pytest.approx(0.30)


def test_record_snapshot_uses_now_ms_when_not_passed(monkeypatch, tmp_path):
    """Not passing captured_at_ms stamps with now() in UTC."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    before = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
        )
        ts = conn.execute(
            "SELECT captured_at_ms FROM vol_snapshots"
        ).fetchone()[0]
    after = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    assert before <= ts <= after


# ---- read_recent_per_day -----------------------------------------------


def test_read_empty_store_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        assert read_recent_per_day(conn, symbol="NVDA", lookback_days=252) == []


def test_read_collapses_same_day_to_latest_write(monkeypatch, tmp_path):
    """Multiple writes on the same NY trading day — latest ms wins."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    t_morning = _ms_at(2026, 4, 23, 9, 35)
    t_afternoon = _ms_at(2026, 4, 23, 15, 55)
    with connect() as conn:
        record_snapshot(conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
                        atm_strike=200.0, atm_expiry="2026-05-01",
                        atm_dte=9, captured_at_ms=t_morning)
        record_snapshot(conn, symbol="NVDA", spot=201.0, atm_iv=0.35,
                        atm_strike=200.0, atm_expiry="2026-05-01",
                        atm_dte=9, captured_at_ms=t_afternoon)
        series = read_recent_per_day(conn, symbol="NVDA", lookback_days=252)
    assert series == [pytest.approx(0.35)]


def test_read_returns_one_entry_per_distinct_ny_trading_day(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        for day_of_month, iv in [(20, 0.30), (21, 0.31), (22, 0.32), (23, 0.33)]:
            record_snapshot(
                conn, symbol="NVDA", spot=200.0, atm_iv=iv,
                atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
                captured_at_ms=_ms_at(2026, 4, day_of_month),
            )
        series = read_recent_per_day(conn, symbol="NVDA", lookback_days=252)
    assert series == pytest.approx([0.30, 0.31, 0.32, 0.33])


def test_read_applies_lookback_limit(monkeypatch, tmp_path):
    """More days stored than requested → only the latest N are returned."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        for i in range(10):
            record_snapshot(
                conn, symbol="NVDA", spot=200.0, atm_iv=0.30 + i * 0.01,
                atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
                captured_at_ms=_ms_at(2026, 4, 14 + i),
            )
        series = read_recent_per_day(conn, symbol="NVDA", lookback_days=3)
    # Latest 3 trading days' values — i=7,8,9 → 0.37, 0.38, 0.39.
    assert series == pytest.approx([0.37, 0.38, 0.39])


def test_v1_to_v2_migration_adds_source_column(monkeypatch, tmp_path):
    """A DB created under schema v1 (no `source` column) should receive
    the column on reopen, and existing rows default to 'observed'."""
    import sqlite3 as _sqlite3

    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    # Hand-write a v1 database — no `source` column, v1 schema version.
    db = tmp_path / "vol_history.db"
    conn = _sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE vol_snapshots (
            captured_at_ms  INTEGER NOT NULL,
            symbol          TEXT    NOT NULL,
            spot            REAL    NOT NULL,
            atm_iv          REAL    NOT NULL,
            atm_strike      REAL    NOT NULL,
            atm_expiry      TEXT    NOT NULL,
            atm_dte         INTEGER NOT NULL,
            PRIMARY KEY (captured_at_ms, symbol)
        );
    """)
    conn.execute(
        "INSERT INTO vol_snapshots VALUES (?,?,?,?,?,?,?)",
        (_ms_at(2026, 3, 1), "NVDA", 150.0, 0.40, 150.0, "2026-05-01", 60),
    )
    conn.commit()
    conn.close()

    # Reopen via our migration-aware connect().
    with connect() as c:
        row = c.execute(
            "SELECT source FROM vol_snapshots WHERE symbol='NVDA'"
        ).fetchone()
        version = c.execute("SELECT version FROM schema_version").fetchone()[0]

    assert row["source"] == "observed"
    assert version == 2


def test_read_isolates_by_symbol(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.35,
            atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=_ms_at(2026, 4, 23),
        )
        record_snapshot(
            conn, symbol="AAPL", spot=200.0, atm_iv=0.99,
            atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
            captured_at_ms=_ms_at(2026, 4, 23),
        )
        nvda = read_recent_per_day(conn, symbol="NVDA", lookback_days=252)
        aapl = read_recent_per_day(conn, symbol="AAPL", lookback_days=252)
    assert nvda == [pytest.approx(0.35)]
    assert aapl == [pytest.approx(0.99)]
