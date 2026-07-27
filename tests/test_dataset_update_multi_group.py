"""``group_name`` is the per-row data-product discriminator. The cron
iterates the UNION of all active groups and dispatches per-symbol
based on membership.

Three subscription combinations matter:
  * (sym, 'volatility')   — vol snapshot only, no implicit ohlcv
  * (sym, 'ohlcv')        — ohlcv cache only, no vol snapshot
  * both rows for sym     — both products written
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from schwab_cli.storage import vol_history, ohlcv_history
from schwab_cli.storage.groups import GROUP_OHLCV, GROUP_VOLATILITY
from schwab_cli.dataset.update import run_volatility_update


def _subscribe(conn, symbol: str, group: str) -> None:
    conn.execute(
        "INSERT INTO subscriptions "
        "(symbol, group_name, source, source_key, subscribed_at) "
        "VALUES (?, ?, 'position', '1234', 1700000000000)",
        (symbol, group),
    )


def _fake_chain(*_a, **_kw):
    return {
        "underlying": {"last": 100.0},
        "expiries": [{
            "expiry": "2026-06-19", "dte": 35,
            "contracts": [{"strike": 100.0, "iv": 0.25, "volume": 100,
                           "type": "call", "delta": 0.5}],
        }],
    }


def _fake_history(client, symbol, **_kw):
    return {
        "candles": [
            {"datetime": 1_700_000_000_000 + i * 86_400_000,
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
             "volume": 1_000_000}
            for i in range(110)
        ]
    }


def _vol_snapshot_count(conn, symbol: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM vol_snapshots WHERE symbol = ?",
        (symbol,),
    ).fetchone()[0]


def test_ohlcv_only_symbol_caches_but_writes_no_vol_snapshot(
    monkeypatch, tmp_path,
):
    """A subscription with only `(sym, 'ohlcv')` populates the cache
    but does NOT write a vol_snapshots row — and never calls get_chain."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _subscribe(conn, "SPX", GROUP_OHLCV)
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain",
               side_effect=_fake_chain) as mock_chain, \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history) as mock_hist, \
         vol_history.connect() as conn:
        summary = run_volatility_update(
            conn, client=MagicMock(),
            now_ms=1_700_000_000_000, accounts=[],
        )

    mock_chain.assert_not_called()
    assert mock_hist.call_count == 1
    with vol_history.connect() as conn:
        assert ohlcv_history.last_cached_day(conn, symbol="SPX") is not None
        assert _vol_snapshot_count(conn, "SPX") == 0
    assert "SPX" not in summary["sampled"]


def test_vol_only_symbol_writes_snapshot_and_caches_ohlcv_for_hv(
    monkeypatch, tmp_path,
):
    """A subscription with only `(sym, 'volatility')` writes a vol
    snapshot. OHLCV cache is still populated because HV computation
    needs closes — the fetch is a side effect, not driven by an
    ohlcv-group row."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _subscribe(conn, "TSLA", GROUP_VOLATILITY)
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain",
               side_effect=_fake_chain) as mock_chain, \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history), \
         vol_history.connect() as conn:
        run_volatility_update(
            conn, client=MagicMock(),
            now_ms=1_700_000_000_000, accounts=[],
        )

    mock_chain.assert_called_once()
    with vol_history.connect() as conn:
        assert _vol_snapshot_count(conn, "TSLA") == 1


def test_both_groups_writes_snapshot_and_caches_with_single_history_call(
    monkeypatch, tmp_path,
):
    """A symbol subscribed to BOTH groups gets a vol snapshot AND
    OHLCV cache populated — the history pull is shared (not called
    twice)."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _subscribe(conn, "NVDA", GROUP_VOLATILITY)
        _subscribe(conn, "NVDA", GROUP_OHLCV)
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain",
               side_effect=_fake_chain), \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history) as mock_hist, \
         vol_history.connect() as conn:
        run_volatility_update(
            conn, client=MagicMock(),
            now_ms=1_700_000_000_000, accounts=[],
        )

    # OHLCV branch hits the cache once. Vol branch sees the cache is
    # already populated and skips its own fetch — total: 1 history call.
    assert mock_hist.call_count == 1
    with vol_history.connect() as conn:
        assert _vol_snapshot_count(conn, "NVDA") == 1
        assert ohlcv_history.last_cached_day(conn, symbol="NVDA") is not None


def test_non_trading_day_skips_all_writes(monkeypatch, tmp_path):
    """INC-1 gate: a weekend/holiday run must sample nothing and write no
    rows, rather than persist stale/sentinel-derived data."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _subscribe(conn, "TSLA", GROUP_VOLATILITY)
        conn.commit()

    # 1779000000000 ms = 2026-05-17 (Sunday ET) — the incident day.
    sunday_ms = 1_779_000_000_000
    with patch("schwab_cli.dataset.update.get_chain",
               side_effect=_fake_chain) as mock_chain, \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history) as mock_hist, \
         vol_history.connect() as conn:
        summary = run_volatility_update(
            conn, client=MagicMock(), now_ms=sunday_ms, accounts=[],
        )

    mock_chain.assert_not_called()
    mock_hist.assert_not_called()
    assert summary["skipped_non_trading_day"] == "2026-05-17"
    with vol_history.connect() as conn:
        assert _vol_snapshot_count(conn, "TSLA") == 0


def test_non_trading_day_override_still_runs(monkeypatch, tmp_path):
    """A deliberate backfill can bypass the gate with require_trading_day."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _subscribe(conn, "TSLA", GROUP_VOLATILITY)
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain",
               side_effect=_fake_chain), \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history), \
         vol_history.connect() as conn:
        summary = run_volatility_update(
            conn, client=MagicMock(), now_ms=1_779_000_000_000, accounts=[],
            require_trading_day=False,
        )
    assert "skipped_non_trading_day" not in summary
