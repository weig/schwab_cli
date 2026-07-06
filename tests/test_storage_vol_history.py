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
    _SCHEMA_VERSION,
    SOURCE_OBSERVED,
    SOURCE_SYNTHETIC,
    connect,
    db_path,
    read_recent_per_day,
    read_recent_per_day_with_source,
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
    # Reopening always migrates up to the current schema version.
    assert version == _SCHEMA_VERSION


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


# ---- read path: non-positive atm_iv must be excluded -------------------
#
# When a -999.0 sentinel (or any non-positive value) slips into storage
# (e.g. via an earlier version of flatten_chain that didn't guard it),
# read_recent_per_day and read_recent_per_day_with_source must exclude
# those rows.  They are inserted here via direct SQL so the test
# exercises the READ filter independently of any guard that may later be
# added to record_snapshot itself.


def test_read_recent_per_day_excludes_non_positive_atm_iv(monkeypatch, tmp_path):
    """read_recent_per_day must skip rows where atm_iv <= 0.

    Uses direct SQL insert to place a -9.99 row (the stored form of the
    -999.0 sentinel after /100) without relying on record_snapshot's
    future validation.
    """
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    t_bad  = _ms_at(2026, 4, 20)
    t_good1 = _ms_at(2026, 4, 21)
    t_good2 = _ms_at(2026, 4, 22)

    with connect() as conn:
        # Insert a "dirty" row that simulates the pre-fix bug: -9.99 in atm_iv.
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'NVDA', 200.0, -9.99, 200.0, '2026-07-18', 79, 'observed')
            """,
            (t_bad,),
        )
        record_snapshot(
            conn, symbol="NVDA", spot=201.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=t_good1,
        )
        record_snapshot(
            conn, symbol="NVDA", spot=202.0, atm_iv=0.35,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=t_good2,
        )
        series = read_recent_per_day(conn, symbol="NVDA", lookback_days=252)

    # Only the two positive values must appear — -9.99 is excluded.
    assert pytest.approx([0.30, 0.35]) == series


def test_read_recent_per_day_with_source_excludes_non_positive_atm_iv(
    monkeypatch, tmp_path
):
    """read_recent_per_day_with_source must also skip atm_iv <= 0 rows."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    t_bad   = _ms_at(2026, 4, 20)
    t_good1 = _ms_at(2026, 4, 21)

    with connect() as conn:
        # Dirty row with the stored sentinel value.
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'SPY', 500.0, -9.99, 500.0, '2026-07-18', 79, 'observed')
            """,
            (t_bad,),
        )
        record_snapshot(
            conn, symbol="SPY", spot=502.0, atm_iv=0.18,
            atm_strike=500.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=t_good1,
        )
        tagged = read_recent_per_day_with_source(
            conn, symbol="SPY", lookback_days=252
        )

    # Must only return the valid positive row, with its source tag.
    assert len(tagged) == 1
    iv, src = tagged[0]
    assert iv == pytest.approx(0.18)
    assert src == SOURCE_OBSERVED


def test_read_recent_per_day_excludes_zero_atm_iv(monkeypatch, tmp_path):
    """Zero IV (non-positive) must also be excluded from the read path."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    t_zero = _ms_at(2026, 4, 20)
    t_good = _ms_at(2026, 4, 21)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'QQQ', 400.0, 0.0, 400.0, '2026-07-18', 79, 'observed')
            """,
            (t_zero,),
        )
        record_snapshot(
            conn, symbol="QQQ", spot=401.0, atm_iv=0.22,
            atm_strike=400.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=t_good,
        )
        series = read_recent_per_day(conn, symbol="QQQ", lookback_days=252)

    assert series == [pytest.approx(0.22)]


# ---- delete_implausible_iv_snapshots -----------------------------------
#
# Implementer note: add `delete_implausible_iv_snapshots(conn) -> int`
# to schwab_cli/storage/vol_history.py.  It must:
#   - DELETE all vol_snapshots rows where atm_iv <= 0
#   - Return the count of rows deleted
#   - Leave rows with atm_iv > 0 untouched
#
# This function is needed as a one-time cleanup for existing databases
# that were polluted by the pre-fix code path.


def test_delete_implausible_iv_snapshots_removes_non_positive_rows(
    monkeypatch, tmp_path
):
    """delete_implausible_iv_snapshots must remove all atm_iv <= 0 rows
    across all symbols and return the count deleted."""
    # Import here so the test fails with ImportError (not AttributeError)
    # when the function hasn't been implemented yet — clear RED signal.
    from schwab_cli.storage.vol_history import delete_implausible_iv_snapshots

    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))

    with connect() as conn:
        # Insert 3 valid rows across two symbols.
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=_ms_at(2026, 4, 21),
        )
        record_snapshot(
            conn, symbol="NVDA", spot=201.0, atm_iv=0.35,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=_ms_at(2026, 4, 22),
        )
        record_snapshot(
            conn, symbol="SPY", spot=500.0, atm_iv=0.18,
            atm_strike=500.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=_ms_at(2026, 4, 21),
        )
        # Insert 2 dirty rows (one per symbol) via direct SQL.
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'NVDA', 200.0, -9.99, 200.0, '2026-07-18', 79, 'observed')
            """,
            (_ms_at(2026, 4, 20),),
        )
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'SPY', 500.0, -9.99, 500.0, '2026-07-18', 79, 'observed')
            """,
            (_ms_at(2026, 4, 20),),
        )

        # Must delete the 2 bad rows and return 2.
        deleted = delete_implausible_iv_snapshots(conn)

    assert deleted == 2

    # Confirm only the 3 valid rows remain.
    with connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM vol_snapshots"
        ).fetchone()[0]
        bad_remaining = conn.execute(
            "SELECT COUNT(*) FROM vol_snapshots WHERE atm_iv <= 0"
        ).fetchone()[0]

    assert remaining == 3
    assert bad_remaining == 0


def test_delete_implausible_iv_snapshots_returns_zero_when_no_bad_rows(
    monkeypatch, tmp_path
):
    """Returns 0 and leaves the DB untouched when all rows are valid."""
    from schwab_cli.storage.vol_history import delete_implausible_iv_snapshots

    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))

    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=_ms_at(2026, 4, 21),
        )
        deleted = delete_implausible_iv_snapshots(conn)

    assert deleted == 0

    with connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM vol_snapshots"
        ).fetchone()[0]
    assert remaining == 1


def test_migration_v5_to_v6_purges_non_positive_iv(monkeypatch, tmp_path):
    """Opening a pre-v6 DB that contains the -999 sentinel (atm_iv <= 0)
    auto-purges those rows via the v5->v6 migration and bumps the version."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))

    # First connect creates a fresh DB (already at the current version);
    # seed a valid row + a dirty sentinel row, then roll the recorded
    # schema version back to 5 to simulate an older store.
    with connect() as conn:
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
            atm_strike=200.0, atm_expiry="2026-07-18", atm_dte=79,
            captured_at_ms=_ms_at(2026, 4, 21),
        )
        conn.execute(
            """
            INSERT INTO vol_snapshots
                (captured_at_ms, symbol, spot, atm_iv,
                 atm_strike, atm_expiry, atm_dte, source)
            VALUES (?, 'NVDA', 200.0, -9.99, 200.0, '2026-07-18', 79, 'observed')
            """,
            (_ms_at(2026, 4, 20),),
        )
        conn.execute("UPDATE schema_version SET version = 5")

    # Reconnecting runs _migrate: v5 -> v6 cleanup purges the sentinel row.
    with connect() as conn:
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        rows = conn.execute(
            "SELECT atm_iv FROM vol_snapshots WHERE symbol = 'NVDA'"
        ).fetchall()

    assert version == _SCHEMA_VERSION  # migrated up to current, past v6 purge
    assert len(rows) == 1
    assert rows[0]["atm_iv"] > 0
