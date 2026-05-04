"""Tests for the transactions cache SQLite layer."""

from __future__ import annotations

from datetime import datetime

import pytest

from schwab_cli.storage import transactions_history as th


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    return tmp_path


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


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


# Plain trade with no fees (e.g. a fractional-share TRADE).
_SAMPLE_TXN = {
    "activityId": 117990367020,
    "time": "2026-04-30T20:49:16+00:00",
    "type": "TRADE",
    "status": "VALID",
    "subAccount": "MARGIN",
    "accountNumber": "57410756",
    "tradeDate": "2026-04-30T04:00:00+00:00",
    "settlementDate": "2026-04-30T04:00:00+00:00",
    "netAmount": -2.72,
    "description": "JPMORGAN CHASE & CO",
    "transferItems": [{
        "instrument": {"assetType": "EQUITY", "symbol": "JPM"},
        "amount": 0.0087,
        "cost": -2.72,
        "price": 312.65,
        "positionEffect": "OPENING",
    }],
}

# Real CRCL option trade (verified live, activityId 114419061931).
# Has multiple fee legs — used to verify total_fees / gross_amount math.
_FEE_BEARING_TXN = {
    "activityId": 114419061931,
    "time": "2026-03-16T15:23:52+00:00",
    "type": "TRADE",
    "status": "VALID",
    "subAccount": "MARGIN",
    "accountNumber": "57410756",
    "tradeDate": "2026-03-16T15:23:52+00:00",
    "netAmount": 1329.34,
    "transferItems": [
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
         "amount": 0.65, "cost": -0.65, "feeType": "COMMISSION"},
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
         "amount": 0.0, "cost": 0.0, "feeType": "SEC_FEE"},
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
         "amount": 0.01, "cost": -0.01, "feeType": "OPT_REG_FEE"},
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
         "amount": 0.0, "cost": 0.0, "feeType": "TAF_FEE"},
        {"instrument": {"assetType": "OPTION", "symbol": "CRCL  270115P00085000"},
         "amount": -1.0, "cost": 1330.0, "price": 13.3,
         "positionEffect": "OPENING"},
    ],
}


def test_upsert_inserts_a_new_row(tmp_storage):
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        rows = conn.execute(
            "SELECT activity_id, type, symbol, net_amount FROM transactions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["activity_id"] == 117990367020
    assert rows[0]["type"] == "TRADE"
    assert rows[0]["symbol"] == "JPM"
    assert rows[0]["net_amount"] == -2.72


def test_upsert_idempotent_on_same_activity_id(tmp_storage):
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert n == 1


def test_upsert_updates_changed_fields(tmp_storage):
    """Schwab status flip (PENDING → VALID) on the same activity_id
    must replace the row, not insert a duplicate."""
    pending = dict(_SAMPLE_TXN, status="PENDING", netAmount=-2.50)
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [pending])
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])  # status=VALID, amt=-2.72
        row = conn.execute(
            "SELECT status, net_amount FROM transactions"
        ).fetchone()
    assert row["status"] == "VALID"
    assert row["net_amount"] == -2.72


def test_upsert_collapses_same_activity_id_across_accounts(tmp_storage):
    """``activity_id`` is the PRIMARY KEY (Schwab guarantees global
    uniqueness across accounts for one user). If the same activity
    ever does appear under two account_hashes, the latter write wins
    and the row's ``account_hash`` reflects the most recent caller."""
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        th.upsert_many(conn, "HASH-B", [_SAMPLE_TXN])
        rows = conn.execute(
            "SELECT activity_id, account_hash FROM transactions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["account_hash"] == "HASH-B"


def test_upsert_extracts_total_fees_and_gross_amount(tmp_storage):
    """Real CRCL option trade has 4 fee legs summing to -0.66.
    ``gross_amount`` = the non-fee leg's cost (1330.00).
    ``net_amount`` = 1329.34 (gross + fees, matches Schwab's payload)."""
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_FEE_BEARING_TXN])
        row = conn.execute(
            "SELECT net_amount, gross_amount, total_fees FROM transactions"
        ).fetchone()
    assert row["net_amount"] == 1329.34
    assert row["gross_amount"] == 1330.0
    assert row["total_fees"] == pytest.approx(-0.66, abs=1e-9)
    assert row["gross_amount"] + row["total_fees"] == pytest.approx(
        row["net_amount"], abs=1e-9,
    )


def test_upsert_zero_fees_when_no_fee_legs(tmp_storage):
    """Fractional-share TRADE with no fee legs → total_fees == 0.0
    (NOT None). None means 'no transferItems at all'."""
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        row = conn.execute(
            "SELECT total_fees, gross_amount FROM transactions"
        ).fetchone()
    assert row["total_fees"] == 0.0
    assert row["gross_amount"] == -2.72


def test_read_range_returns_payloads_in_window(tmp_storage):
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        out = th.read_range(
            conn, account_hash="HASH-A",
            start_ms=_ms("2026-04-01T00:00:00+00:00"),
            end_ms=_ms("2026-05-01T00:00:00+00:00"),
        )
    assert len(out) == 1
    assert out[0]["activityId"] == 117990367020
    assert out[0]["type"] == "TRADE"


def test_read_range_excludes_outside_window(tmp_storage):
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        out = th.read_range(
            conn, account_hash="HASH-A",
            start_ms=_ms("2026-01-01T00:00:00+00:00"),
            end_ms=_ms("2026-01-31T00:00:00+00:00"),
        )
    assert out == []


def test_read_range_inclusive_boundaries(tmp_storage):
    with th.connect() as conn:
        th.upsert_many(conn, "HASH-A", [_SAMPLE_TXN])
        exact_ms = _ms("2026-04-30T20:49:16+00:00")
        out = th.read_range(
            conn, account_hash="HASH-A",
            start_ms=exact_ms, end_ms=exact_ms,
        )
    assert len(out) == 1


# ---- Coverage tracking ----------------------------------------------------

def test_coverage_starts_empty(tmp_storage):
    with th.connect() as conn:
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == []


def test_merge_coverage_inserts_first_range(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=200)
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == [(100, 200)]


def test_merge_coverage_merges_overlapping(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=200)
        th.merge_coverage(conn, "HASH-A", start_ms=150, end_ms=250)
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == [(100, 250)]


def test_merge_coverage_merges_adjacent(tmp_storage):
    """Adjacent ranges (end+1 == next start) merge into one."""
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=200)
        th.merge_coverage(conn, "HASH-A", start_ms=201, end_ms=300)
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == [(100, 300)]


def test_merge_coverage_keeps_disjoint(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=200)
        th.merge_coverage(conn, "HASH-A", start_ms=500, end_ms=600)
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == [(100, 200), (500, 600)]


def test_merge_coverage_subsumes_contained(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=300)
        th.merge_coverage(conn, "HASH-A", start_ms=150, end_ms=250)
        cov = th.read_coverage(conn, account_hash="HASH-A")
    assert cov == [(100, 300)]


def test_merge_coverage_scoped_per_account(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=200)
        th.merge_coverage(conn, "HASH-B", start_ms=100, end_ms=200)
        a = th.read_coverage(conn, account_hash="HASH-A")
        b = th.read_coverage(conn, account_hash="HASH-B")
    assert a == [(100, 200)]
    assert b == [(100, 200)]


def test_coverage_gaps_full_miss(tmp_storage):
    with th.connect() as conn:
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=100, end_ms=200,
        )
    assert gaps == [(100, 200)]


def test_coverage_gaps_full_hit(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=50, end_ms=300)
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=100, end_ms=200,
        )
    assert gaps == []


def test_coverage_gaps_partial_left(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=150, end_ms=250)
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=100, end_ms=200,
        )
    assert gaps == [(100, 149)]


def test_coverage_gaps_partial_right(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=150)
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=120, end_ms=200,
        )
    assert gaps == [(151, 200)]


def test_coverage_gaps_middle_hole(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=150)
        th.merge_coverage(conn, "HASH-A", start_ms=200, end_ms=250)
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=100, end_ms=250,
        )
    assert gaps == [(151, 199)]


def test_coverage_gaps_multi_holes(tmp_storage):
    with th.connect() as conn:
        th.merge_coverage(conn, "HASH-A", start_ms=100, end_ms=120)
        th.merge_coverage(conn, "HASH-A", start_ms=140, end_ms=160)
        th.merge_coverage(conn, "HASH-A", start_ms=180, end_ms=200)
        gaps = th.coverage_gaps(
            conn, account_hash="HASH-A", start_ms=100, end_ms=200,
        )
    assert gaps == [(121, 139), (161, 179)]
