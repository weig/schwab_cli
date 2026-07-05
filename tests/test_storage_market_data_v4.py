"""Schema v4: active volatility subs are mirrored into ohlcv rows.
Idempotent — re-runs do not duplicate."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.storage.groups import GROUP_OHLCV, GROUP_VOLATILITY


def _seed_v3_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(vol_history._SCHEMA_DDL)
    conn.execute("INSERT INTO schema_version VALUES (3)")
    conn.executemany(
        "INSERT INTO subscriptions "
        "(symbol, group_name, source, source_key, subscribed_at, unsubscribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("AAPL", "volatility", "position", "1234", 1700000000000, None),
            ("MSFT", "volatility", "indices",  "SPX",  1700000000000, None),
            ("TSLA", "volatility", "position", "1234", 1600000000000, 1650000000000),
        ],
    )
    conn.commit()
    conn.close()


def test_v4_mirrors_active_volatility_to_ohlcv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    _seed_v3_db(db)

    with vol_history.connect() as conn:
        ohlcv = {r["symbol"] for r in conn.execute(
            "SELECT symbol FROM subscriptions "
            "WHERE group_name = ? AND unsubscribed_at IS NULL",
            (GROUP_OHLCV,),
        ).fetchall()}

    assert ohlcv == {"AAPL", "MSFT"}


def test_v4_migration_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    _seed_v3_db(db)

    with vol_history.connect() as _:
        pass
    with vol_history.connect() as conn:
        n = conn.execute(
            "SELECT count(*) FROM subscriptions WHERE group_name = ?",
            (GROUP_OHLCV,),
        ).fetchone()[0]
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
    assert n == 2
    assert version == vol_history._SCHEMA_VERSION  # migrated up to current


def test_v4_preserves_volatility_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    _seed_v3_db(db)

    with vol_history.connect() as conn:
        vols = sorted(r["symbol"] for r in conn.execute(
            "SELECT symbol FROM subscriptions WHERE group_name = ?",
            (GROUP_VOLATILITY,),
        ).fetchall())
    assert vols == ["AAPL", "MSFT", "TSLA"]


def test_connect_renames_legacy_vol_history_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    legacy = tmp_path / "vol_history.db"
    new    = tmp_path / "market_data.db"
    _seed_v3_db(legacy)
    (tmp_path / "vol_history.db-wal").write_bytes(b"")
    (tmp_path / "vol_history.db-shm").write_bytes(b"")
    monkeypatch.setattr(vol_history, "storage_dir", lambda: tmp_path)
    monkeypatch.setattr(vol_history, "db_path", lambda: new)

    with vol_history.connect() as _:
        pass

    assert new.exists()
    assert not legacy.exists()
    assert not (tmp_path / "vol_history.db-wal").exists()
    assert not (tmp_path / "vol_history.db-shm").exists()


def test_connect_refuses_when_both_files_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    legacy = tmp_path / "vol_history.db"
    new    = tmp_path / "market_data.db"
    _seed_v3_db(legacy)
    _seed_v3_db(new)
    monkeypatch.setattr(vol_history, "storage_dir", lambda: tmp_path)
    monkeypatch.setattr(vol_history, "db_path", lambda: new)

    with pytest.raises(RuntimeError, match="both files exist"):
        with vol_history.connect() as _:
            pass
