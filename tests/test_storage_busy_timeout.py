"""Regression: writer-vs-writer contention on the shared SQLite file
used to crash one of the scheduler's parallel children with
``OperationalError: database is locked``. Now both writers wait their
turn via a 30s busy_timeout."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.storage import transactions_history


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    return tmp_path


def test_vol_history_connection_has_30s_busy_timeout(isolated_storage):
    """Pin the timeout so a future refactor can't silently revert
    to the SQLite default (which is 0 — instant lock error)."""
    with vol_history.connect() as conn:
        timeout_ms = conn.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]
    assert timeout_ms == 30000


def test_transactions_history_connection_has_30s_busy_timeout(
    isolated_storage,
):
    with transactions_history.connect() as conn:
        timeout_ms = conn.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]
    assert timeout_ms == 30000


def test_concurrent_writers_dont_raise_locked(isolated_storage):
    """The real-world bug: two writers holding their own transactions
    against the same DB. Before the fix, one would raise
    ``OperationalError: database is locked``. After: both succeed,
    serialized by SQLite under the hood via the busy_timeout."""
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def writer(payload_id: int) -> None:
        try:
            barrier.wait(timeout=5)
            with vol_history.connect() as conn:
                conn.execute(
                    "INSERT INTO vol_snapshots "
                    "(captured_at_ms, symbol, spot, atm_iv, "
                    " atm_strike, atm_expiry, atm_dte) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        1_700_000_000_000 + payload_id,
                        f"SYM{payload_id}",
                        100.0 + payload_id, 0.3, 100.0,
                        "2026-12-19", 30,
                    ),
                )
                # Hold the write transaction briefly so the OTHER
                # writer is forced to wait — that's the path
                # busy_timeout protects.
                time.sleep(0.1)
        except sqlite3.OperationalError as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(i,))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"expected no lock errors, got: {errors!r}"
