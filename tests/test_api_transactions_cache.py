"""Tests for the cached-fetch orchestrator.

Two layers under test:
  * ``_fresh_cutoff(today)`` — pure function, calendar math.
  * ``fetch_cached(...)`` — the orchestrator. Mocks the raw
    ``get_transactions`` call so we can assert API hits.

Storage uses a real on-disk SQLite via SCHWAB_CLI_STORAGE → tmp_path.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from schwab_cli.api.transactions_cache import _fresh_cutoff, fetch_cached
from schwab_cli.storage import transactions_history as th


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    return tmp_path


@pytest.fixture
def patched_get_transactions(monkeypatch):
    """Patch the raw API call. Returns the call-log list."""
    def factory(stub):
        calls: list[tuple[datetime, datetime]] = []

        def patched(client, account_hash, *, start, end, types=None, symbol=None):
            calls.append((start, end))
            return stub(start, end)

        monkeypatch.setattr(
            "schwab_cli.api.transactions_cache.get_transactions", patched,
        )
        return calls
    return factory


def _fake_client():
    client = MagicMock()
    client.account_ids.return_value = [
        MagicMock(account_number="0756", hash_value="HASH-A"),
    ]
    client.resolve_account.return_value = MagicMock(
        account_number="0756", hash_value="HASH-A",
    )
    return client


def _txn(activity_id: int, time_iso: str) -> dict:
    return {
        "activityId": activity_id,
        "time": time_iso,
        "type": "TRADE",
        "status": "VALID",
        "tradeDate": time_iso,
        "netAmount": -1.23,
        "transferItems": [{
            "instrument": {"assetType": "EQUITY", "symbol": "JPM"},
            "amount": 1.0, "price": 100.0, "cost": -1.23,
        }],
    }


# ---- _fresh_cutoff math ---------------------------------------------------

def test_fresh_cutoff_mid_month_returns_first_of_prev_month():
    """May 4 → Apr 1 (first of prev month, comfortably ≥30 days back)."""
    assert _fresh_cutoff(date(2026, 5, 4)) == date(2026, 4, 1)


def test_fresh_cutoff_first_of_month_returns_first_of_prev_month():
    """May 1 → Apr 1 (exactly 30 days back, satisfies ≥30 rule)."""
    assert _fresh_cutoff(date(2026, 5, 1)) == date(2026, 4, 1)


def test_fresh_cutoff_last_day_of_month_returns_first_of_prev_month():
    """May 31 → Apr 1 (60 days fresh)."""
    assert _fresh_cutoff(date(2026, 5, 31)) == date(2026, 4, 1)


def test_fresh_cutoff_year_boundary():
    """Jan 1 → Dec 1 of prior year."""
    assert _fresh_cutoff(date(2026, 1, 1)) == date(2025, 12, 1)


def test_fresh_cutoff_february_edge_pushes_back():
    """Mar 1 → first-of-prev = Feb 1 (only 28 days back). The
    ≥30-days rule pushes the cutoff back to Jan 30."""
    assert _fresh_cutoff(date(2026, 3, 1)) == date(2026, 1, 30)


def test_fresh_cutoff_late_march_uses_first_of_prev_month():
    """By Mar 31, first-of-prev (Feb 1) gives 58 days back — well
    above the 30-day floor. Use Feb 1."""
    assert _fresh_cutoff(date(2026, 3, 31)) == date(2026, 2, 1)


# ---- old-only range (range entirely below cutoff) -------------------------

def test_old_range_cold_cache_fetches_once(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Range entirely in 'old' territory: first call hits API for the
    full range; second call hits zero APIs (pure cache)."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    calls = patched_get_transactions(
        lambda s, e: [_txn(1, "2026-01-15T10:00:00+00:00")]
    )
    client = _fake_client()
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    n_first = len(calls)
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert n_first == 1
    assert len(calls) == n_first  # second call is pure cache


# ---- fresh-only range (range entirely above cutoff) -----------------------

def test_fresh_range_always_fetches_even_when_cached(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Range entirely in 'fresh' territory: every call hits the API,
    even when coverage already includes it."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    calls = patched_get_transactions(
        lambda s, e: [_txn(int(s.timestamp()), s.isoformat())]
    )
    client = _fake_client()
    # Apr 15 → May 4 is entirely in the fresh window (cutoff = Apr 1)
    fetch_cached(
        client, "0756",
        start=datetime(2026, 4, 15, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    n_first = len(calls)
    fetch_cached(
        client, "0756",
        start=datetime(2026, 4, 15, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    # Two calls means each invocation hit the API.
    assert n_first == 1
    assert len(calls) == 2


# ---- straddling range (spans the cutoff) ---------------------------------

def test_straddling_range_caches_old_part_fetches_fresh(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Range straddling the cutoff: old part cached after first call;
    fresh part re-fetched every time."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    fetched_windows: list[tuple[datetime, datetime]] = []
    patched_get_transactions(
        lambda s, e: (fetched_windows.append((s, e)) or [])
    )
    client = _fake_client()
    # Mar 1 → May 4 straddles cutoff (Apr 1).
    # Expected on cold cache: 2 calls (old [Mar 1, Apr 1], fresh (Apr 1, May 4]).
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    n_after_cold = len(fetched_windows)
    assert n_after_cold == 2
    # Second call: old part cached, fresh part re-fetched.
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    new_calls = fetched_windows[n_after_cold:]
    assert len(new_calls) == 1  # fresh part only


# ---- chunking -------------------------------------------------------------

def test_long_old_range_chunked_into_60_day_windows(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Cold cache, 200-day range entirely in 'old' → 4 chunks (60+60+60+20)."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 12, 31),  # cutoff = Nov 1; everything is old
    )
    fetched_windows: list[tuple[datetime, datetime]] = []
    patched_get_transactions(
        lambda s, e: (fetched_windows.append((s, e)) or [])
    )
    client = _fake_client()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=200)
    fetch_cached(client, "0756", start=start, end=end)
    assert len(fetched_windows) == 4
    for s, e in fetched_windows:
        assert (e - s) <= timedelta(days=60)


# ---- refresh flag ---------------------------------------------------------

def test_refresh_bypasses_split_and_fetches_full_range(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """``refresh=True`` ignores the cutoff: fetches the entire requested
    range as one big gap."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    fetched_windows: list[tuple[datetime, datetime]] = []
    patched_get_transactions(
        lambda s, e: (fetched_windows.append((s, e)) or [])
    )
    client = _fake_client()
    # Warm cache for the old half so a non-refresh call would be small.
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    n_warm = len(fetched_windows)
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        refresh=True,
    )
    new_calls = fetched_windows[n_warm:]
    # refresh=True forces the whole range; that's at least 2 chunks
    # (Mar 1 → May 4 = 64 days = 2 × 60-day chunks).
    assert len(new_calls) >= 2


# ---- API-side type filter -------------------------------------------------

def test_fetch_cached_has_no_types_kwarg():
    """The cache always fetches the full set; ``types`` is a local-side
    filter only. ``fetch_cached`` does NOT accept a types kwarg."""
    import inspect
    sig = inspect.signature(fetch_cached)
    assert "types" not in sig.parameters
    assert "type_filter" not in sig.parameters


# ---- multi-account --------------------------------------------------------

def test_multi_account_iterates_each_account_independently(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Cold cache + two accounts → one call per account."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    fetched_windows: list[tuple[datetime, datetime]] = []
    patched_get_transactions(
        lambda s, e: (fetched_windows.append((s, e)) or [])
    )
    client = MagicMock()
    client.account_ids.return_value = [
        MagicMock(account_number="0756", hash_value="HASH-A"),
        MagicMock(account_number="1234", hash_value="HASH-B"),
    ]
    fetch_cached(
        client, None,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert len(fetched_windows) == 2


# ---- end-to-end storage round-trip ----------------------------------------

def test_fetched_data_is_stored_for_future_reads(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """After fetch_cached returns, the same data is queryable directly
    via the storage layer (no double-fetch needed)."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    patched_get_transactions(
        lambda s, e: [_txn(42, "2026-01-15T10:00:00+00:00")]
    )
    client = _fake_client()
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    with th.connect() as conn:
        ms_start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        ms_end = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
        out = th.read_range(
            conn, account_hash="HASH-A",
            start_ms=ms_start, end_ms=ms_end,
        )
    assert any(t["activityId"] == 42 for t in out)


# ---- cache stats out-param ------------------------------------------------

def test_stats_populated_for_cold_cache(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Cold cache, all-old range → all rows came from API; from_cache=0."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    patched_get_transactions(
        lambda s, e: [_txn(1, "2026-01-15T10:00:00+00:00")]
    )
    client = _fake_client()
    stats: dict = {}
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        stats=stats,
    )
    assert stats["total"] == 1
    assert stats["from_api"] == 1
    assert stats["from_cache"] == 0


def test_stats_populated_for_warm_cache(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Warm cache, all-old range → all rows from cache; from_api=0."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    patched_get_transactions(
        lambda s, e: [_txn(1, "2026-01-15T10:00:00+00:00")]
    )
    client = _fake_client()
    # Warm the cache.
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    # Second call hits cache only.
    stats: dict = {}
    fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        stats=stats,
    )
    assert stats["total"] == 1
    assert stats["from_api"] == 0
    assert stats["from_cache"] == 1


def test_stats_split_for_straddling_range(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Range crosses cutoff. After warmup, old part is cached and fresh
    part still re-fetches → from_cache reflects old portion."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    # Stub returns one transaction per fetch, time inside the requested window.
    def stub(s, e):
        return [_txn(int(s.timestamp()), s.isoformat())]
    patched_get_transactions(stub)
    client = _fake_client()
    # Warm: Mar 1 → May 4 straddles cutoff (Apr 1).
    # Cold call yields 2 rows (one for each gap chunk).
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    stats: dict = {}
    fetch_cached(
        client, "0756",
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        stats=stats,
    )
    # Second call: only fresh gap re-fetches → from_api=1, total=2,
    # from_cache=1.
    assert stats["total"] >= 1
    assert stats["from_api"] >= 1
    assert stats["from_cache"] == stats["total"] - stats["from_api"]


def test_stats_optional_default_none_no_op(
    tmp_storage, patched_get_transactions, monkeypatch,
):
    """Omitting ``stats`` doesn't crash. Existing callers still work."""
    monkeypatch.setattr(
        "schwab_cli.api.transactions_cache._today",
        lambda: date(2026, 5, 4),
    )
    patched_get_transactions(
        lambda s, e: [_txn(1, "2026-01-15T10:00:00+00:00")]
    )
    client = _fake_client()
    rows = fetch_cached(
        client, "0756",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert isinstance(rows, list)
