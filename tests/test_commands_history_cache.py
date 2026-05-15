"""For subscribed symbols at daily interval, `history` reads from
ohlcv_daily before falling back to Schwab. For un-subscribed symbols
or non-daily intervals, behavior is unchanged (live API call)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from schwab_cli.storage import vol_history, ohlcv_history
from schwab_cli.storage.groups import GROUP_OHLCV
from schwab_cli.commands import history as history_cmd


def _seed_subscribed(conn, symbol: str) -> None:
    conn.execute(
        "INSERT INTO subscriptions "
        "(symbol, group_name, source, source_key, subscribed_at) "
        "VALUES (?, ?, 'position', '1234', 1700000000000)",
        (symbol, GROUP_OHLCV),
    )


def test_subscribed_daily_with_full_cache_does_not_call_api(
    monkeypatch, tmp_path,
):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _seed_subscribed(conn, "AAPL")
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=[
            {"day": "2026-05-12", "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000,
             "captured_at_ms": 1747000000000},
            {"day": "2026-05-13", "open": 101.0, "high": 103.0,
             "low": 100.5, "close": 102.5, "volume": 1_200_000,
             "captured_at_ms": 1747100000000},
        ])
        conn.commit()

    with patch("schwab_cli.commands.history.get_history") as m_api, \
         patch("schwab_cli.commands.history._client") as m_client:
        m_client.return_value = MagicMock()
        try:
            history_cmd.run(
                symbol="AAPL",
                range_str="20260512..20260513",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit:
            pass  # typer.Exit from successful rendering

    m_api.assert_not_called()


def test_unsubscribed_falls_back_to_api(monkeypatch, tmp_path):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as _:
        pass

    fake_response = {
        "candles": [
            {"datetime": 1747000000000, "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000}
        ],
        "symbol": "UNKNOWN",
    }
    with patch("schwab_cli.commands.history.get_history",
               return_value=fake_response) as m_api, \
         patch("schwab_cli.commands.history._client") as m_client:
        m_client.return_value = MagicMock()
        try:
            history_cmd.run(
                symbol="UNKNOWN",
                range_str="20260512..20260513",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit:
            pass

    m_api.assert_called_once()
