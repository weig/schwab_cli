"""End-to-end orchestrator tests.

We mock at the boundary: the indices fetcher and the SchwabClient
chain/positions calls. The orchestrator's job is to walk the
``index_subscriptions`` / ``subscriptions`` tables, diff against
upstream, and apply soft-deletes / inserts. This test simulates one
full weekly run.
"""
from __future__ import annotations

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.dataset.store import (
    subscribe_index, list_active_subscriptions,
)
from schwab_cli.dataset.update import run_indices_update


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as c:
        yield c


def test_indices_update_inserts_initial_members(conn, monkeypatch):
    subscribe_index(conn, index_name="SPX", group_name="volatility",
                    captured_at_ms=1000)

    def fake_fetch(index_name, *, client):
        assert index_name == "SPX"
        return {"AAPL", "MSFT", "NVDA"}

    monkeypatch.setattr(
        "schwab_cli.dataset.update.fetch_index_members", fake_fetch
    )

    summary = run_indices_update(conn, http_client=None,
                                  group_name="volatility",
                                  now_ms=2000)
    assert sorted(summary["SPX"]["added"]) == ["AAPL", "MSFT", "NVDA"]
    assert summary["SPX"]["removed"] == []

    rows = list_active_subscriptions(conn, group_name="volatility")
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT", "NVDA"}


def test_indices_update_diffs_against_existing(conn, monkeypatch):
    subscribe_index(conn, index_name="SPX", group_name="volatility",
                    captured_at_ms=1000)
    # Pre-populate with two existing index members + one stale.
    conn.execute(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
        ("OLD", "volatility", "indices", "SPX", 500, None),
    )
    conn.execute(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
        ("AAPL", "volatility", "indices", "SPX", 500, None),
    )

    def fake_fetch(index_name, *, client):
        return {"AAPL", "NEW1", "NEW2"}

    monkeypatch.setattr(
        "schwab_cli.dataset.update.fetch_index_members", fake_fetch
    )
    summary = run_indices_update(conn, http_client=None,
                                  group_name="volatility",
                                  now_ms=2000)
    assert sorted(summary["SPX"]["added"]) == ["NEW1", "NEW2"]
    assert summary["SPX"]["removed"] == ["OLD"]

    row = conn.execute(
        "SELECT unsubscribed_at FROM subscriptions "
        "WHERE symbol='OLD' AND source='indices'"
    ).fetchone()
    assert row["unsubscribed_at"] == 2000


def test_indices_update_logs_provider_failure_continues(conn, monkeypatch):
    subscribe_index(conn, index_name="SPX", group_name="volatility")
    subscribe_index(conn, index_name="DJI", group_name="volatility")

    def fake_fetch(index_name, *, client):
        if index_name == "SPX":
            raise RuntimeError("all providers failed")
        return {"BA", "CAT"}

    monkeypatch.setattr(
        "schwab_cli.dataset.update.fetch_index_members", fake_fetch
    )

    summary = run_indices_update(conn, http_client=None,
                                  group_name="volatility",
                                  now_ms=2000)
    assert summary["SPX"].get("error") is not None
    assert sorted(summary["DJI"]["added"]) == ["BA", "CAT"]


def test_indices_update_skips_rut_with_todo_log(conn, monkeypatch):
    subscribe_index(conn, index_name="RUT", group_name="volatility")

    def fake_fetch(index_name, *, client):
        from schwab_cli.dataset.indices import fetch_index_members as real
        return real(index_name, client=None)

    monkeypatch.setattr(
        "schwab_cli.dataset.update.fetch_index_members", fake_fetch
    )

    summary = run_indices_update(conn, http_client=None,
                                  group_name="volatility",
                                  now_ms=2000)
    assert "TODO" in summary["RUT"]["error"]


from schwab_cli.dataset.update import sync_account_positions


def test_sync_account_positions_inserts_new_holdings(conn, monkeypatch):
    def fake_get_account(client, account_hash):
        return {
            "securitiesAccount": {
                "positions": [
                    {"instrument": {"assetType": "OPTION",
                                    "underlyingSymbol": "NVDA"},
                     "longQuantity": 1, "shortQuantity": 0},
                    {"instrument": {"assetType": "OPTION",
                                    "underlyingSymbol": "AMZN"},
                     "longQuantity": 0, "shortQuantity": 2},
                ]
            }
        }

    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_account", fake_get_account
    )

    summary = sync_account_positions(
        conn, client=None, account_hash="abcd1234efgh",
        group_name="volatility", now_ms=1000,
    )
    assert sorted(summary["added"]) == ["AMZN", "NVDA"]
    rows = list_active_subscriptions(conn, group_name="volatility")
    sources = [(r["symbol"], r["source_key"]) for r in rows]
    assert ("NVDA", "efgh") in sources


def test_sync_account_positions_soft_deletes_closed(conn, monkeypatch):
    conn.execute(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
        ("OLD", "volatility", "position", "efgh", 100, None),
    )
    conn.execute(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
        ("NVDA", "volatility", "position", "efgh", 100, None),
    )

    def fake_get_account(client, account_hash):
        return {"securitiesAccount": {"positions": [
            {"instrument": {"assetType": "OPTION",
                            "underlyingSymbol": "NVDA"},
             "longQuantity": 1, "shortQuantity": 0},
        ]}}

    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_account", fake_get_account
    )

    summary = sync_account_positions(
        conn, client=None, account_hash="abcdefgh",
        group_name="volatility", now_ms=2000,
    )
    assert summary["closed"] == ["OLD"]
    closed_row = conn.execute(
        "SELECT unsubscribed_at FROM subscriptions "
        "WHERE symbol='OLD' AND source='position'"
    ).fetchone()
    assert closed_row["unsubscribed_at"] == 2000
