"""Tests for the transactions cache SQLite layer.

Schema, migrations, and the basic ``connect()`` contract. Coverage
helpers and upsert tests live in later test modules — this one is
about the on-disk layout."""

from __future__ import annotations

import pytest

from schwab_cli.storage import transactions_history as th


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    return tmp_path


def test_db_path_lives_under_storage_dir(tmp_storage):
    assert th.db_path() == tmp_storage / "account.db"


def test_connect_creates_tables_and_schema_version(tmp_storage):
    with th.connect() as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "transactions" in names
    assert "transactions_coverage" in names
    assert "schema_version" in names


def test_connect_is_idempotent(tmp_storage):
    """Two opens in a row must not raise (re-running migrations is a no-op)."""
    with th.connect():
        pass
    with th.connect():
        pass


def test_schema_version_recorded(tmp_storage):
    with th.connect() as conn:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row is not None
    assert row[0] >= 1
