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
    """Equities (stocks / ETFs) and options-bearing underlyings should
    both produce subscriptions rows. Mutual funds and cash are skipped."""
    def fake_get_account(client, account_hash):
        return {
            "securitiesAccount": {
                "positions": [
                    # Plain stock holding.
                    {"instrument": {"assetType": "EQUITY",
                                    "symbol": "TSLA"},
                     "longQuantity": 100, "shortQuantity": 0},
                    # ETF (also EQUITY in Schwab's classification).
                    {"instrument": {"assetType": "EQUITY",
                                    "symbol": "SPY"},
                     "longQuantity": 50, "shortQuantity": 0},
                    # Long call on NVDA.
                    {"instrument": {"assetType": "OPTION",
                                    "underlyingSymbol": "NVDA"},
                     "longQuantity": 1, "shortQuantity": 0},
                    # Short put on AMZN.
                    {"instrument": {"assetType": "OPTION",
                                    "underlyingSymbol": "AMZN"},
                     "longQuantity": 0, "shortQuantity": 2},
                    # Mutual fund — must be skipped.
                    {"instrument": {"assetType": "MUTUAL_FUND",
                                    "symbol": "VFIAX"},
                     "longQuantity": 100, "shortQuantity": 0},
                    # Cash equivalent — must be skipped.
                    {"instrument": {"assetType": "CASH_EQUIVALENT",
                                    "symbol": "MMDA1"},
                     "longQuantity": 1234.56, "shortQuantity": 0},
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
    # Stocks (TSLA, SPY) + option underlyings (NVDA, AMZN). VFIAX +
    # MMDA1 dropped.
    assert sorted(summary["added"]) == ["AMZN", "NVDA", "SPY", "TSLA"]
    rows = list_active_subscriptions(conn, group_name="volatility")
    sources = [(r["symbol"], r["source_key"]) for r in rows]
    assert ("TSLA", "efgh") in sources
    assert ("SPY",  "efgh") in sources
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


def test_run_volatility_update_partial_results_survive_crash(
    conn, monkeypatch, tmp_path,
):
    """If the orchestrator dies mid-run, the rows it had already
    committed must survive on a fresh connection. Periodic commits
    inside the for-loop are what give us this guarantee — without
    them a single transaction at the end of the connect() context
    manager would roll the whole batch back.
    """
    from schwab_cli.dataset.store import subscribe_equity
    from schwab_cli.dataset.update import _COMMIT_BATCH
    from schwab_cli.storage import vol_history
    import json

    # Subscribe more symbols than _COMMIT_BATCH so we cross a flush.
    n_symbols = _COMMIT_BATCH + 5
    symbols = [f"SYM{i:03d}" for i in range(n_symbols)]
    for s in symbols:
        subscribe_equity(conn, symbol=s, group_name="volatility",
                         captured_at_ms=1000)

    fake_chain = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_nvda_full.json")
        .read_text()
    )

    crash_at_index = _COMMIT_BATCH + 2  # past one flush, before final
    call_count = {"n": 0}

    def fake_get_chain(client, symbol, **kwargs):
        call_count["n"] += 1
        if call_count["n"] > crash_at_index:
            raise RuntimeError("simulated network blip")
        return fake_chain

    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_chain", fake_get_chain
    )
    monkeypatch.setattr(
        "schwab_cli.dataset.update.get_history",
        lambda c, s, **kw: {"candles": [{"close": 200.0 + i * 0.1}
                                        for i in range(60)]},
    )

    summary = run_volatility_update(
        conn, client=None, group_name="volatility",
        now_ms=_ms(2026, 4, 15),
        accounts=[],
    )

    # Sample succeeded for the first crash_at_index symbols, then
    # errored for the rest.
    assert len(summary["sampled"]) == crash_at_index
    assert len(summary["errors"]) == n_symbols - crash_at_index

    # Open a fresh connection — only the rows committed before the
    # crash should be visible. The first flush at _COMMIT_BATCH had
    # already happened, so we expect at least _COMMIT_BATCH rows
    # to have survived.
    with vol_history.connect() as conn2:
        n_rows = conn2.execute(
            "SELECT COUNT(*) FROM vol_snapshots WHERE symbol LIKE 'SYM%'"
        ).fetchone()[0]
    assert n_rows >= _COMMIT_BATCH, (
        f"only {n_rows} rows committed; the periodic flush at "
        f"every _COMMIT_BATCH={_COMMIT_BATCH} symbols isn't firing"
    )


def test_run_volatility_update_skips_already_sampled_today(conn, monkeypatch):
    """If a symbol already has a FULL daily snapshot (atm_iv_30d) for
    today's NY day — a prior scheduled run already recorded it — a cron
    RE-RUN must skip it (idempotent; double-write wastes API calls and
    storage). An ad-hoc `vol` partial snapshot does NOT count (see
    test_dedup_counts_only_full_daily_snapshot)."""
    from schwab_cli.dataset.store import subscribe_equity
    from schwab_cli.storage.vol_history import record_extended_snapshot

    subscribe_equity(conn, symbol="NVDA", group_name="volatility")
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="ACTIVE", tier_since=_ms(2026, 1, 1),
        consecutive_days_below=0, last_evaluated_at=_ms(2026, 1, 1),
    )
    # Pre-seed today's FULL daily snapshot (atm_iv_30d present) — a prior
    # scheduled run already captured today.
    record_extended_snapshot(
        conn, symbol="NVDA", spot=200.0, atm_iv=0.34,
        atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
        captured_at_ms=_ms(2026, 4, 15, hour=13),  # 9am NY
        source="observed", atm_iv_30d=0.33,
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
