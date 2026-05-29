"""Dataset store CRUD — exercises the new tables introduced in v3.

All tests use the real SQLite via vol_history.connect() so the
schema migration is exercised together with the queries. monkeypatched
SCHWAB_CLI_STORAGE points at tmp_path so production data stays
untouched.
"""
from __future__ import annotations

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.dataset.store import (
    DatasetFreshness,
    read_dataset_freshness,
    subscribe_equity,
    unsubscribe_equity,
    list_active_subscriptions,
)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as c:
        yield c


# ---- read_dataset_freshness -------------------------------------------


def test_read_dataset_freshness_empty_db(conn):
    """An empty DB yields all-None — MAX over no rows is NULL."""
    fresh = read_dataset_freshness(conn)
    assert fresh == DatasetFreshness(
        ohlcv_ms=None, volatility_ms=None, account_ms=None
    )


def test_read_dataset_freshness_returns_max_per_table(conn):
    """Returns the latest captured_at_ms per tracked table."""
    conn.execute(
        "INSERT INTO ohlcv_daily "
        "(symbol, day, open, high, low, close, volume, captured_at_ms) "
        "VALUES ('AAPL', '2026-01-02', 1, 2, 0, 1, 10, 111)"
    )
    conn.execute(
        "INSERT INTO ohlcv_daily "
        "(symbol, day, open, high, low, close, volume, captured_at_ms) "
        "VALUES ('AAPL', '2026-01-03', 1, 2, 0, 1, 10, 222)"
    )
    conn.execute(
        "INSERT INTO vol_snapshots "
        "(captured_at_ms, symbol, spot, atm_iv, atm_strike, atm_expiry, atm_dte) "
        "VALUES (333, 'AAPL', 100, 0.2, 100, '2026-02-20', 30)"
    )
    conn.execute(
        "INSERT INTO account_nav_daily "
        "(account_hash, day, market_value, cash, total_value, captured_at_ms) "
        "VALUES ('abcd', '2026-01-02', 100, 10, 110, 444)"
    )

    fresh = read_dataset_freshness(conn)

    assert isinstance(fresh, DatasetFreshness)
    assert fresh.ohlcv_ms == 222
    assert fresh.volatility_ms == 333
    assert fresh.account_ms == 444


def test_subscribe_equity_inserts_row(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["source"] == "equity"


def test_subscribe_equity_idempotent(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=2000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1


def test_unsubscribe_soft_deletes(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    unsubscribe_equity(conn, symbol="NVDA", group_name="volatility",
                       captured_at_ms=5000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert active == []
    all_rows = conn.execute(
        "SELECT * FROM subscriptions WHERE symbol='NVDA'"
    ).fetchall()
    assert len(all_rows) == 1
    assert all_rows[0]["unsubscribed_at"] == 5000


def test_resubscribe_clears_unsubscribed_at(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    unsubscribe_equity(conn, symbol="NVDA", group_name="volatility",
                       captured_at_ms=5000)
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=9000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert len(active) == 1
    assert active[0]["unsubscribed_at"] is None


from schwab_cli.dataset.store import (
    subscribe_index,
    unsubscribe_index,
    list_active_index_subscriptions,
    subscribe_position,
    unsubscribe_position,
)


def test_subscribe_index_inserts_row(conn):
    subscribe_index(conn, index_name="SPX", group_name="volatility",
                    captured_at_ms=1000)
    rows = list_active_index_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1
    assert rows[0]["index_name"] == "SPX"


def test_subscribe_index_rejects_unknown_index(conn):
    with pytest.raises(ValueError, match="not in supported index set"):
        subscribe_index(conn, index_name="EFA", group_name="volatility")


def test_subscribe_index_accepts_supported_set(conn):
    for name in ("SPX", "DJI", "NQ", "RUT"):
        subscribe_index(conn, index_name=name, group_name="volatility")
    rows = list_active_index_subscriptions(conn, group_name="volatility")
    assert {r["index_name"] for r in rows} == {"SPX", "DJI", "NQ", "RUT"}


def test_subscribe_position_inserts_row(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="1234", captured_at_ms=1000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert rows[0]["source"] == "position"
    assert rows[0]["source_key"] == "1234"


def test_unsubscribe_position_only_targets_one_account(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="1234")
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="5678")
    unsubscribe_position(conn, symbol="NVDA", group_name="volatility",
                         account_hash_last4="1234", captured_at_ms=9000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert len(active) == 1
    assert active[0]["source_key"] == "5678"


from schwab_cli.dataset.store import (
    last_close_at_for_symbol,
    sources_for_symbol,
)


def test_sources_for_symbol_aggregates(conn):
    subscribe_equity(conn, symbol="AMZN", group_name="volatility",
                     captured_at_ms=1)
    subscribe_position(conn, symbol="AMZN", group_name="volatility",
                       account_hash_last4="1234", captured_at_ms=2)
    out = sources_for_symbol(conn, symbol="AMZN", group_name="volatility")
    assert out == {"equity", "position"}


def test_sources_excludes_unsubscribed(conn):
    subscribe_equity(conn, symbol="AMZN", group_name="volatility",
                     captured_at_ms=1)
    unsubscribe_equity(conn, symbol="AMZN", group_name="volatility",
                       captured_at_ms=5)
    out = sources_for_symbol(conn, symbol="AMZN", group_name="volatility")
    assert out == set()


# ---- indices grace window after removal -------------------------------
#
# Index members removed by the weekly cron stay in the working set for
# 30 days so the IV trail is captured all the way through the exit; once
# past 30 days they drop out naturally on the next sample run.

_DAY_MS = 86_400_000
_GRACE_MS = 30 * _DAY_MS


def test_list_active_includes_indices_within_grace(conn):
    subscribe_index(conn, index_name="SPX", group_name="volatility")
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES ('NVDA', 'volatility', 'indices', 'SPX', 1000, ?)
        """,
        (10 * _DAY_MS,),
    )
    now_ms = 10 * _DAY_MS + 5 * _DAY_MS  # 5 days after removal
    rows = list_active_subscriptions(
        conn, group_name="volatility", now_ms=now_ms,
    )
    assert any(r["symbol"] == "NVDA" for r in rows)


def test_list_active_drops_indices_past_grace(conn):
    subscribe_index(conn, index_name="SPX", group_name="volatility")
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES ('NVDA', 'volatility', 'indices', 'SPX', 1000, ?)
        """,
        (10 * _DAY_MS,),
    )
    now_ms = 10 * _DAY_MS + _GRACE_MS + _DAY_MS  # past grace
    rows = list_active_subscriptions(
        conn, group_name="volatility", now_ms=now_ms,
    )
    assert not any(r["symbol"] == "NVDA" for r in rows)


def test_list_active_grace_only_for_indices_source(conn):
    """Grace is indices-only — equity / position unsubscribes drop immediately."""
    subscribe_equity(conn, symbol="EQ", group_name="volatility",
                     captured_at_ms=1000)
    unsubscribe_equity(conn, symbol="EQ", group_name="volatility",
                       captured_at_ms=10 * _DAY_MS)
    now_ms = 10 * _DAY_MS + 5 * _DAY_MS
    rows = list_active_subscriptions(
        conn, group_name="volatility", now_ms=now_ms,
    )
    assert not any(r["symbol"] == "EQ" for r in rows)


def test_list_active_without_now_ms_keeps_strict_filter(conn):
    """Backward compat: caller that doesn't pass now_ms gets old behavior."""
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES ('NVDA', 'volatility', 'indices', 'SPX', 1000, ?)
        """,
        (10 * _DAY_MS,),
    )
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert not any(r["symbol"] == "NVDA" for r in rows)


def test_sources_for_symbol_includes_indices_within_grace(conn):
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES ('NVDA', 'volatility', 'indices', 'SPX', 1000, ?)
        """,
        (10 * _DAY_MS,),
    )
    now_ms = 10 * _DAY_MS + 5 * _DAY_MS
    out = sources_for_symbol(
        conn, symbol="NVDA", group_name="volatility", now_ms=now_ms,
    )
    assert "indices" in out


def test_sources_for_symbol_drops_indices_past_grace(conn):
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES ('NVDA', 'volatility', 'indices', 'SPX', 1000, ?)
        """,
        (10 * _DAY_MS,),
    )
    now_ms = 10 * _DAY_MS + _GRACE_MS + _DAY_MS
    out = sources_for_symbol(
        conn, symbol="NVDA", group_name="volatility", now_ms=now_ms,
    )
    assert "indices" not in out


def test_last_close_at_picks_most_recent(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="aaaa", captured_at_ms=1)
    unsubscribe_position(conn, symbol="NVDA", group_name="volatility",
                         account_hash_last4="aaaa", captured_at_ms=100)
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="bbbb", captured_at_ms=50)
    unsubscribe_position(conn, symbol="NVDA", group_name="volatility",
                         account_hash_last4="bbbb", captured_at_ms=200)
    out = last_close_at_for_symbol(conn, symbol="NVDA",
                                   group_name="volatility")
    assert out == 200


def test_last_close_at_none_when_active(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="aaaa", captured_at_ms=1)
    out = last_close_at_for_symbol(conn, symbol="NVDA",
                                   group_name="volatility")
    assert out is None


from schwab_cli.dataset.store import (
    read_ticker_state, write_ticker_state, list_ticker_states,
)


def test_read_ticker_state_returns_none_for_missing(conn):
    out = read_ticker_state(conn, symbol="X", group_name="volatility")
    assert out is None


def test_write_then_read_round_trip(conn):
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="ACTIVE", tier_since=1000,
        consecutive_days_below=3, last_evaluated_at=2000,
    )
    out = read_ticker_state(conn, symbol="NVDA", group_name="volatility")
    assert out["tier"] == "ACTIVE"
    assert out["consecutive_days_below"] == 3


def test_write_ticker_state_is_upsert(conn):
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="GRACE", tier_since=1000,
        consecutive_days_below=0, last_evaluated_at=1000,
    )
    write_ticker_state(
        conn, symbol="NVDA", group_name="volatility",
        tier="ACTIVE", tier_since=2000,
        consecutive_days_below=0, last_evaluated_at=2000,
    )
    out = read_ticker_state(conn, symbol="NVDA", group_name="volatility")
    assert out["tier"] == "ACTIVE"
    assert out["tier_since"] == 2000


def test_list_ticker_states_filters_by_tier(conn):
    for sym, tier in [("A", "ACTIVE"), ("B", "WATCH"), ("C", "ACTIVE")]:
        write_ticker_state(
            conn, symbol=sym, group_name="volatility",
            tier=tier, tier_since=0,
            consecutive_days_below=0, last_evaluated_at=0,
        )
    actives = list_ticker_states(conn, group_name="volatility", tier="ACTIVE")
    assert {r["symbol"] for r in actives} == {"A", "C"}


from schwab_cli.dataset.store import read_status_rows


def test_read_status_rows_aggregates_sources(conn):
    from schwab_cli.dataset.store import (
        subscribe_equity, subscribe_position,
    )
    subscribe_equity(conn, symbol="AMZN", group_name="volatility",
                     captured_at_ms=1000)
    subscribe_position(conn, symbol="AMZN", group_name="volatility",
                       account_hash_last4="1234", captured_at_ms=2000)
    rows = read_status_rows(conn, group_name="volatility")
    amzn = next(r for r in rows if r["symbol"] == "AMZN")
    assert sorted(amzn["sources"]) == [
        "equity", "position=1234"
    ]
    assert amzn["subscribed_at"] == 1000  # earliest


def test_read_status_rows_filters_by_tier(conn):
    from schwab_cli.dataset.store import (
        subscribe_equity, write_ticker_state,
    )
    subscribe_equity(conn, symbol="A", group_name="volatility")
    subscribe_equity(conn, symbol="B", group_name="volatility")
    write_ticker_state(conn, symbol="A", group_name="volatility",
                       tier="ACTIVE", tier_since=0,
                       consecutive_days_below=0, last_evaluated_at=0)
    write_ticker_state(conn, symbol="B", group_name="volatility",
                       tier="WATCH", tier_since=0,
                       consecutive_days_below=0, last_evaluated_at=0)
    rows = read_status_rows(conn, group_name="volatility", tier="ACTIVE")
    assert {r["symbol"] for r in rows} == {"A"}
