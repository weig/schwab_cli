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


from schwab_cli.dataset.update import sync_account_positions, run_volatility_update


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


# ---- run_volatility_update tests --------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import json

from schwab_cli.dataset.store import write_ticker_state, read_ticker_state


def _ms(year, month, day, hour=22):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc)
               .timestamp() * 1000)


def test_run_volatility_update_samples_active_writes_row(
    conn, monkeypatch
):
    from schwab_cli.dataset.store import subscribe_equity
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=_ms(2026, 1, 1))
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="GRACE", tier_since=_ms(2026, 1, 1),
        consecutive_days_below=0, last_evaluated_at=_ms(2026, 1, 1),
    )

    fake_chain = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_nvda_full.json")
        .read_text()
    )

    def fake_get_chain(client, symbol, **kwargs):
        return fake_chain

    def fake_get_history(client, symbol, **kwargs):
        return {"candles": [{"close": 200.0 + i * 0.1}
                           for i in range(60)]}

    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_chain", fake_get_chain
    )
    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_history", fake_get_history
    )

    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 1, 5),
        accounts=[],
    )
    assert summary["sampled"] == ["NVDA"]
    assert summary["skipped"] == []

    row = conn.execute(
        "SELECT * FROM vol_snapshots WHERE symbol='NVDA'"
    ).fetchone()
    assert row is not None
    assert row["atm_iv_30d"] is not None


def test_run_volatility_update_skips_already_sampled_today(conn, monkeypatch):
    """If a symbol already has an observed row for today's NY day, the
    daily cron must skip — same-day double-write is a waste of API
    calls and storage. Regression test for the behaviour."""
    from schwab_cli.dataset.store import subscribe_equity
    from schwab_cli.storage.vol_history import record_extended_snapshot

    subscribe_equity(conn, symbol="NVDA", group_name="volatility")
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="ACTIVE", tier_since=_ms(2026, 1, 1),
        consecutive_days_below=0, last_evaluated_at=_ms(2026, 1, 1),
    )
    # Pre-seed today's observed row at NY 9:35am 2026-04-15 — i.e. an
    # earlier `vol NVDA` invocation already captured today.
    record_extended_snapshot(
        conn, symbol="NVDA", spot=200.0, atm_iv=0.34,
        atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
        captured_at_ms=_ms(2026, 4, 15, hour=13),  # 9am NY
        source="observed",
    )

    chain_calls: list[str] = []
    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_chain",
        lambda *a, **kw: chain_calls.append("called") or {},
    )

    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 4, 15, hour=22),  # 6pm NY same day
        accounts=[],
    )
    assert summary["sampled"] == []
    assert "NVDA" in summary["skipped"]
    # Crucially, we never made the chain pull — saved an API call.
    assert chain_calls == []


def test_run_volatility_update_skips_frozen(conn, monkeypatch):
    from schwab_cli.dataset.store import subscribe_equity
    subscribe_equity(conn, symbol="NVDA", group_name="volatility")
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="FROZEN", tier_since=_ms(2026, 1, 1),
        consecutive_days_below=99, last_evaluated_at=_ms(2026, 1, 1),
    )

    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 4, 15),
        accounts=[],
    )
    assert "NVDA" in summary["skipped"]
    assert summary["sampled"] == []


def test_run_volatility_update_watch_only_on_monday(conn, monkeypatch):
    from schwab_cli.dataset.store import subscribe_equity
    subscribe_equity(conn, symbol="NVDA", group_name="volatility")
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="WATCH", tier_since=_ms(2026, 1, 1),
        consecutive_days_below=8, last_evaluated_at=_ms(2026, 1, 1),
    )

    # Tuesday 2026-04-14 — WATCH should be skipped.
    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 4, 14),
        accounts=[],
    )
    assert summary["skipped"] == ["NVDA"]

    # Monday 2026-04-13 — WATCH gets sampled.
    fake_chain = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_nvda_full.json")
        .read_text()
    )
    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_chain",
        lambda client, sym, **kw: fake_chain,
    )
    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_history",
        lambda client, sym, **kw: {"candles": [{"close": 200.0}] * 60},
    )
    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 4, 13),
        accounts=[],
    )
    assert summary["sampled"] == ["NVDA"]
