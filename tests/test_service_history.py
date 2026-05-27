"""Unit tests for the Layer-2 ``service.history.get_history`` function.

These exercise the service in isolation from the command shim. The stable
``schwab_cli.api.history.get_history`` seam is mocked so no real HTTP happens,
and the cache helpers' storage seams (``vol_history.connect`` +
``ohlcv_history.*``) are mocked so no real DB is touched. A future-dated
session keeps ``service.auth.get_session`` from attempting an ``oauth.refresh``.

Coverage:
  - a daily cache HIT returns without calling the API or touching auth
    (no config/session on disk);
  - a daily cache MISS calls the API and opportunistically upserts;
  - a non-daily interval skips the cache entirely (gap/read_range never
    called) and calls the API;
  - ``NoCandles`` is raised with the exact user-ready message when the
    shaped envelope has no candles;
  - ``NotConfigured`` / ``NotAuthenticated`` propagate on the API path.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.history import NoCandles, get_history
from schwab_cli.service.types import HistoryResult
from schwab_cli.session import Session
from schwab_cli.session import save as save_session


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _prep_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


_CANDLE_0_MS = _ms(datetime(2024, 4, 22, 13, 30, tzinfo=timezone.utc))
_CANDLE_1_MS = _ms(datetime(2024, 4, 23, 13, 30, tzinfo=timezone.utc))

_RAW = {
    "symbol": "AAPL",
    "previousClose": 150.0,
    "candles": [
        {"datetime": _CANDLE_0_MS, "open": 150.25, "high": 152.0,
         "low": 149.8, "close": 151.5, "volume": 2_000_000},
        {"datetime": _CANDLE_1_MS, "open": 151.5, "high": 153.0,
         "low": 150.5, "close": 152.75, "volume": 2_500_000},
    ],
}

_CACHE_ROWS = [
    {"captured_at_ms": _CANDLE_0_MS, "open": 150.25, "high": 152.0,
     "low": 149.8, "close": 151.5, "volume": 2_000_000},
    {"captured_at_ms": _CANDLE_1_MS, "open": 151.5, "high": 153.0,
     "low": 150.5, "close": 152.75, "volume": 2_500_000},
]

# 2024-04-22 .. 2024-04-23 in UTC.
_START = datetime(2024, 4, 22, 0, 0, tzinfo=timezone.utc)
_END = datetime(2024, 4, 23, 23, 59, 59, tzinfo=timezone.utc)


class _FakeConn:
    pass


class _FakeCM:
    def __enter__(self) -> _FakeConn:
        return _FakeConn()

    def __exit__(self, *_: object) -> None:
        pass


def _call(symbol="AAPL", *, frequency_type="daily", frequency=1, label="1day"):
    return get_history(
        symbol,
        frequency_type=frequency_type,
        frequency=frequency,
        label=label,
        start=_START,
        end=_END,
        range_str="20240422..20240423",
    )


def test_cache_hit_returns_without_api_or_auth(monkeypatch, tmp_path):
    """A full daily cache HIT returns a HistoryResult without calling the
    API and without loading config/session (empty HOME)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with (
        patch("schwab_cli.api.history.get_history") as m_api,
        patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
        patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
        patch(
            "schwab_cli.storage.ohlcv_history.read_range",
            return_value=_CACHE_ROWS,
        ),
    ):
        result = _call()
    assert isinstance(result, HistoryResult)
    m_api.assert_not_called()
    dates = [c["datetime"] for c in result.envelope["candles"]]
    assert dates == ["2024-04-22", "2024-04-23"]
    # Cache rows carry no previousClose.
    assert result.envelope["previousClose"] is None


def test_cache_miss_calls_api_and_upserts(monkeypatch, tmp_path):
    """A daily cache MISS hits the API and backfills via upsert_candles."""
    _prep_auth(monkeypatch, tmp_path)
    with (
        patch("schwab_cli.api.history.get_history", return_value=_RAW) as m_api,
        patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
        patch(
            "schwab_cli.storage.ohlcv_history.gap",
            return_value=("2024-04-22", "2024-04-23"),
        ),
        patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
    ):
        result = _call()
    m_api.assert_called_once()
    m_upsert.assert_called_once()
    assert m_upsert.call_args.kwargs["symbol"] == "AAPL"
    assert len(m_upsert.call_args.kwargs["candles"]) == 2
    assert result.envelope["symbol"] == "AAPL"
    assert result.envelope["previousClose"] == 150.0


def test_non_daily_skips_cache(monkeypatch, tmp_path):
    """A non-daily interval never touches the cache and calls the API."""
    _prep_auth(monkeypatch, tmp_path)
    with (
        patch("schwab_cli.api.history.get_history", return_value=_RAW) as m_api,
        patch("schwab_cli.storage.ohlcv_history.gap") as m_gap,
        patch("schwab_cli.storage.ohlcv_history.read_range") as m_read,
        patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
    ):
        result = _call(frequency_type="minute", frequency=5, label="5min")
    m_api.assert_called_once()
    m_gap.assert_not_called()
    m_read.assert_not_called()
    m_upsert.assert_not_called()
    assert isinstance(result, HistoryResult)


def test_no_candles_raises_with_exact_message(monkeypatch, tmp_path):
    """An empty envelope raises NoCandles with the full user-ready sentence."""
    _prep_auth(monkeypatch, tmp_path)
    empty = {"symbol": "AAPL", "candles": []}
    with (
        patch("schwab_cli.api.history.get_history", return_value=empty),
        patch(
            "schwab_cli.storage.ohlcv_history.gap",
            return_value=("2024-04-22", "2024-04-23"),
        ),
        patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
        patch("schwab_cli.storage.ohlcv_history.upsert_candles"),
    ):
        with pytest.raises(NoCandles) as exc:
            _call()
    assert str(exc.value) == (
        "No candles found for AAPL in 20240422..20240423 at 1day."
    )


def test_no_config_raises_not_configured(monkeypatch, tmp_path):
    """On the API path (cache miss) with no config on disk -> NotConfigured."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with (
        patch(
            "schwab_cli.storage.ohlcv_history.gap",
            return_value=("2024-04-22", "2024-04-23"),
        ),
        patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
    ):
        with pytest.raises(NotConfigured):
            _call()


def test_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    """Config present but no session file -> NotAuthenticated propagates."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    with (
        patch(
            "schwab_cli.storage.ohlcv_history.gap",
            return_value=("2024-04-22", "2024-04-23"),
        ),
        patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
    ):
        with pytest.raises(NotAuthenticated):
            _call()
