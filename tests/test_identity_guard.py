"""CUSIP identity guard — storage + orchestration (schema v10)."""
from __future__ import annotations

import pytest

from schwab_cli.storage import identity
from schwab_cli.storage.vol_history import _SCHEMA_VERSION, connect


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def test_schema_v10_table_exists(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "underlying_identity" in tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] \
        == _SCHEMA_VERSION


def test_first_sighting_records_new(conn):
    v = identity.check_and_record_identity(
        conn, symbol="CRWD", cusip="22788C105",
        description="CROWDSTRIKE HLDGS Class A", now_ms=1000)
    assert v == "new"
    assert identity.read_identity(conn, "CRWD")["cusip"] == "22788C105"
    assert not identity.is_quarantined(conn, "CRWD")


def test_unchanged_cusip_is_ok(conn):
    identity.check_and_record_identity(
        conn, symbol="CRWD", cusip="22788C105", description="CROWDSTRIKE",
        now_ms=1000)
    v = identity.check_and_record_identity(
        conn, symbol="CRWD", cusip="22788C105", description="CROWDSTRIKE",
        now_ms=2000)
    assert v == "ok"


def test_reverse_split_updates_cusip_without_quarantine(conn):
    identity.check_and_record_identity(
        conn, symbol="XYZ", cusip="12345A105", description="XYZ CORP",
        now_ms=1000)
    v = identity.check_and_record_identity(
        conn, symbol="XYZ", cusip="12345A204", description="XYZ CORP",
        now_ms=2000)
    assert v == "corporate_action"
    assert identity.read_identity(conn, "XYZ")["cusip"] == "12345A204"  # updated
    assert not identity.is_quarantined(conn, "XYZ")


def test_ticker_reuse_quarantines_and_keeps_old_cusip(conn):
    identity.check_and_record_identity(
        conn, symbol="FIG", cusip="11111A100", description="OLD DELISTED CO",
        now_ms=1000)
    v = identity.check_and_record_identity(
        conn, symbol="FIG", cusip="99999Z900", description="FIGMA INC",
        now_ms=2000)
    assert v == "reuse"
    assert identity.is_quarantined(conn, "FIG")
    # The old company's CUSIP is preserved, NOT overwritten with the new one.
    assert identity.read_identity(conn, "FIG")["cusip"] == "11111A100"
