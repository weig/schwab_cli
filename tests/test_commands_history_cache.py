"""For subscribed symbols at daily interval, `history` reads from
ohlcv_daily before falling back to Schwab. For un-subscribed symbols
or non-daily intervals, behavior is unchanged (live API call)."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.commands import history as history_cmd
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session
from schwab_cli.storage import ohlcv_history, vol_history
from schwab_cli.storage.groups import GROUP_OHLCV


def _prep_auth(monkeypatch, tmp_path) -> None:
    """Config + future-dated session so the service-layer auth path
    (``service.auth.get_session``) does not attempt an ``oauth.refresh``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=int(time.time()) + 3600,
                         refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600))


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

    # Cache HIT — the service must never call the Layer-1 API.
    with patch("schwab_cli.api.history.get_history") as m_api:
        try:
            history_cmd.run(
                symbol="AAPL",
                range_str="20260512..20260513",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit as exc:
            # A cache hit renders successfully and returns normally; if it
            # exits at all it must be a clean exit, never an error (which
            # would make assert_not_called pass for the wrong reason).
            assert exc.code in (0, None), f"cache-hit run failed: exit {exc.code}"

    m_api.assert_not_called()


def test_unsubscribed_falls_back_to_api(monkeypatch, tmp_path):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as _:
        pass
    _prep_auth(monkeypatch, tmp_path)

    fake_response = {
        "candles": [
            {"datetime": 1747000000000, "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000}
        ],
        "symbol": "UNKNOWN",
    }
    with patch("schwab_cli.api.history.get_history",
               return_value=fake_response) as m_api:
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
