"""Characterization tests for the `schwab history` command.

These tests pin the CURRENT observable behaviour of the history command
end-to-end so that the upcoming service-layer migration can be proven
behaviour-preserving without altering production code.

Stable seams used (must survive the refactor):
  - API path:   ``schwab_cli.commands.history.get_history`` (the name bound
                in the commands module via ``from schwab_cli.api.history import
                get_history``).  Patch here to control what the API returns.
  - Cache path: ``schwab_cli.storage.vol_history.connect`` (context-manager),
                ``schwab_cli.storage.ohlcv_history.gap``,
                ``schwab_cli.storage.ohlcv_history.read_range``, and
                ``schwab_cli.storage.ohlcv_history.upsert_candles``.

CRITICAL behaviour pinned:
  - On a cache HIT the client is NEVER built, so a fully-cached daily
    request succeeds with NO config/session on disk.
  - Non-daily intervals skip the cache entirely (no ``gap``/``read_range``
    calls) and hit the API directly.
  - Daily API responses are opportunistically written back to the cache
    via ``upsert_candles``; non-daily responses are not.

Golden values were captured by running the current code and recording its
output verbatim. Do NOT alter golden constants without first verifying that
the production code changed intentionally.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ms(dt: datetime) -> int:
    """Return UTC millisecond epoch for a datetime."""
    return int(dt.timestamp() * 1000)


def _prep(monkeypatch, tmp_path) -> None:
    """Isolated HOME with valid config + non-expired session.

    The session's ``expires_at`` is set to now+3600 so the service-layer
    auth path (``service.auth.get_session``) does NOT attempt a real
    ``oauth.refresh`` — it only mints when the access token looks expired.
    """
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


# ---------------------------------------------------------------------------
# Canned /pricehistory payload
# ---------------------------------------------------------------------------

# Two candles: 2024-04-22 and 2024-04-23 (UTC 13:30 = NY 09:30 EDT).
# previousClose = 150.0 so candle[0].change = 151.5 - 150.0 = +1.50 (+1.00 %)
# candle[1].change = 152.75 - 151.5 = +1.25 (+0.825...)
_CANDLE_0_MS = _ms(datetime(2024, 4, 22, 13, 30, tzinfo=timezone.utc))
_CANDLE_1_MS = _ms(datetime(2024, 4, 23, 13, 30, tzinfo=timezone.utc))

_RAW = {
    "symbol": "AAPL",
    "empty": False,
    "previousClose": 150.0,
    "candles": [
        {
            "datetime": _CANDLE_0_MS,
            "open": 150.25,
            "high": 152.00,
            "low": 149.80,
            "close": 151.50,
            "volume": 2_000_000,
        },
        {
            "datetime": _CANDLE_1_MS,
            "open": 151.50,
            "high": 153.00,
            "low": 150.50,
            "close": 152.75,
            "volume": 2_500_000,
        },
    ],
}

# ---------------------------------------------------------------------------
# Golden constants (captured from current code)
# ---------------------------------------------------------------------------

# JSON envelope
_GOLDEN_JSON_SYMBOL = "AAPL"
_GOLDEN_JSON_INTERVAL = "1day"
_GOLDEN_JSON_FROM = "2024-04-22T09:30:00-04:00"
_GOLDEN_JSON_TO = "2024-04-23T09:30:00-04:00"
_GOLDEN_JSON_PREV_CLOSE = 150.0
_GOLDEN_JSON_TOP_KEYS = {"symbol", "interval", "from", "to", "previousClose", "candles"}
_GOLDEN_JSON_CANDLE_KEYS = {
    "datetime", "open", "high", "low", "close", "volume", "change", "changePct",
}

# candle[0]: change vs previousClose
_GOLDEN_C0_DATE = "2024-04-22"
_GOLDEN_C0_OPEN = 150.25
_GOLDEN_C0_HIGH = 152.0
_GOLDEN_C0_LOW = 149.8
_GOLDEN_C0_CLOSE = 151.5
_GOLDEN_C0_VOLUME = 2_000_000
_GOLDEN_C0_CHANGE = 1.5
_GOLDEN_C0_CHANGE_PCT = 1.0

# candle[1]: change vs candle[0] close
_GOLDEN_C1_DATE = "2024-04-23"
_GOLDEN_C1_CLOSE = 152.75
_GOLDEN_C1_CHANGE = 1.25
# changePct = (152.75 - 151.5) / 151.5 * 100 ≈ 0.825082508250825
_GOLDEN_C1_CHANGE_PCT_APPROX = 0.825

# MD golden strings (captured from current rendering)
_GOLDEN_MD_HEADING = "# AAPL — 1day  2024-04-22 → 2024-04-23"
_GOLDEN_MD_PREV_CLOSE_LINE = "**Previous close:** $150.00 · **Candles:** 2"
_GOLDEN_MD_TABLE_HEADER = "| Date | Open | High | Low | Close | Change | Change% | Volume |"
_GOLDEN_MD_TABLE_SEP = "|------|------|------|-----|-------|--------|---------|--------|"
_GOLDEN_MD_ROW_0 = "| 2024-04-22 | 150.25 | 152.00 | 149.80 | 151.50 | +1.50 | +1.00 | 2,000,000 |"
_GOLDEN_MD_ROW_1 = "| 2024-04-23 | 151.50 | 153.00 | 150.50 | 152.75 | +1.25 | +0.83 | 2,500,000 |"

# HUMAN golden substrings
_GOLDEN_HUMAN_HEADER_SYMBOL = "AAPL"
_GOLDEN_HUMAN_HEADER_INTERVAL = "1day"
_GOLDEN_HUMAN_HEADER_DATE_RANGE = "2024-04-22 → 2024-04-23"
_GOLDEN_HUMAN_CANDLE_COUNT = "2 candles"
_GOLDEN_HUMAN_COL_DATE = "Date"
_GOLDEN_HUMAN_COL_OPEN = "Open"
_GOLDEN_HUMAN_COL_CLOSE = "Close"
_GOLDEN_HUMAN_ROW_DATE = "2024-04-22"

# ---------------------------------------------------------------------------
# Cache-path helpers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal fake SQLite connection (not used directly by tests — just a
    token returned by the context manager)."""


class _FakeCM:
    """Context manager that yields a _FakeConn."""

    def __enter__(self) -> _FakeConn:
        return _FakeConn()

    def __exit__(self, *_: object) -> None:
        pass


def _cache_fake_rows():
    """Two cache rows matching _RAW candles, using the same epoch-ms values."""
    return [
        {
            "captured_at_ms": _CANDLE_0_MS,
            "open": 150.25,
            "high": 152.00,
            "low": 149.80,
            "close": 151.50,
            "volume": 2_000_000,
        },
        {
            "captured_at_ms": _CANDLE_1_MS,
            "open": 151.50,
            "high": 153.00,
            "low": 150.50,
            "close": 152.75,
            "volume": 2_500_000,
        },
    ]


# ===========================================================================
# 1. Golden output — API path (cache miss), daily, all three formats
# ===========================================================================


class TestGoldenOutputApiPath:
    """Pin HUMAN / JSON / MD output for a daily request served from the API."""

    def test_human_exit_0(self, monkeypatch, tmp_path):
        """Happy-path HUMAN output must exit 0."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert result.exit_code == 0, result.output

    def test_human_contains_symbol(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert _GOLDEN_HUMAN_HEADER_SYMBOL in result.output

    def test_human_contains_interval(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert _GOLDEN_HUMAN_HEADER_INTERVAL in result.output

    def test_human_contains_date_range(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert _GOLDEN_HUMAN_HEADER_DATE_RANGE in result.output

    def test_human_contains_candle_count(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert _GOLDEN_HUMAN_CANDLE_COUNT in result.output

    def test_human_contains_table_columns(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        for col in (_GOLDEN_HUMAN_COL_DATE, _GOLDEN_HUMAN_COL_OPEN, _GOLDEN_HUMAN_COL_CLOSE):
            assert col in result.output, f"Missing column header: {col!r}"

    def test_human_contains_row_date(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert _GOLDEN_HUMAN_ROW_DATE in result.output

    # --- JSON ---

    def test_json_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        assert result.exit_code == 0, result.output

    def test_json_top_level_keys(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert set(data.keys()) == _GOLDEN_JSON_TOP_KEYS

    def test_json_symbol(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert data["symbol"] == _GOLDEN_JSON_SYMBOL

    def test_json_interval(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert data["interval"] == _GOLDEN_JSON_INTERVAL

    def test_json_from_to_timestamps(self, monkeypatch, tmp_path):
        """``from`` and ``to`` must be ISO-8601 NY-tz datetime strings."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert data["from"] == _GOLDEN_JSON_FROM
        assert data["to"] == _GOLDEN_JSON_TO

    def test_json_previous_close(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert data["previousClose"] == _GOLDEN_JSON_PREV_CLOSE

    def test_json_candle_count(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert len(data["candles"]) == 2

    def test_json_candle_keys(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        assert set(data["candles"][0].keys()) == _GOLDEN_JSON_CANDLE_KEYS

    def test_json_candle_0_ohlcv(self, monkeypatch, tmp_path):
        """candle[0] OHLCV values must exactly match the canned payload."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        c0 = data["candles"][0]
        assert c0["datetime"] == _GOLDEN_C0_DATE
        assert c0["open"] == _GOLDEN_C0_OPEN
        assert c0["high"] == _GOLDEN_C0_HIGH
        assert c0["low"] == _GOLDEN_C0_LOW
        assert c0["close"] == _GOLDEN_C0_CLOSE
        assert c0["volume"] == _GOLDEN_C0_VOLUME

    def test_json_candle_0_change_vs_previous_close(self, monkeypatch, tmp_path):
        """candle[0].change must be computed against previousClose."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        c0 = data["candles"][0]
        assert c0["change"] == pytest.approx(_GOLDEN_C0_CHANGE, rel=1e-6)
        assert c0["changePct"] == pytest.approx(_GOLDEN_C0_CHANGE_PCT, rel=1e-6)

    def test_json_candle_1_change_vs_candle_0_close(self, monkeypatch, tmp_path):
        """candle[1].change must be computed against candle[0] close."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--json"],
            )
        data = json.loads(result.stdout)
        c1 = data["candles"][1]
        assert c1["close"] == _GOLDEN_C1_CLOSE
        assert c1["change"] == pytest.approx(_GOLDEN_C1_CHANGE, rel=1e-6)
        assert c1["changePct"] == pytest.approx(_GOLDEN_C1_CHANGE_PCT_APPROX, rel=1e-3)

    # --- MD ---

    def test_md_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert result.exit_code == 0, result.output

    def test_md_heading(self, monkeypatch, tmp_path):
        """MD output must contain the exact H1 heading (golden)."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert _GOLDEN_MD_HEADING in result.stdout

    def test_md_prev_close_and_candle_count(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert _GOLDEN_MD_PREV_CLOSE_LINE in result.stdout

    def test_md_table_header(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert _GOLDEN_MD_TABLE_HEADER in result.stdout
        assert _GOLDEN_MD_TABLE_SEP in result.stdout

    def test_md_row_0_exact(self, monkeypatch, tmp_path):
        """MD first data row must match golden format exactly."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert _GOLDEN_MD_ROW_0 in result.stdout

    def test_md_row_1_exact(self, monkeypatch, tmp_path):
        """MD second data row must match golden format exactly."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        assert _GOLDEN_MD_ROW_1 in result.stdout

    def test_md_is_valid_markdown(self, monkeypatch, tmp_path):
        """MD output must start with a '#' heading and contain a pipe table."""
        _prep(monkeypatch, tmp_path)
        with patch("schwab_cli.commands.history.get_history", return_value=_RAW):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day", "--md"],
            )
        lines = result.stdout.splitlines()
        assert lines[0].startswith("# ")
        assert any("|" in ln for ln in lines)


# ===========================================================================
# 2. Cache HIT path
# ===========================================================================


class TestCacheHit:
    """Pin cache-first read behaviour for daily intervals."""

    def test_cache_hit_does_not_call_api(self, monkeypatch, tmp_path):
        """When the cache fully covers the range, ``get_history`` is NOT called."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history") as m_api,
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_api.assert_not_called()

    def test_cache_hit_renders_from_cached_rows(self, monkeypatch, tmp_path):
        """Cache-sourced output must contain the expected candle dates."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history"),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        dates = [c["datetime"] for c in data["candles"]]
        assert "2024-04-22" in dates
        assert "2024-04-23" in dates

    def test_cache_hit_json_candle_count(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history"),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        assert len(data["candles"]) == 2

    def test_cache_hit_no_previous_close_in_json(self, monkeypatch, tmp_path):
        """Cache rows don't carry previousClose — envelope must have null."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history"),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        assert data["previousClose"] is None

    def test_cache_hit_succeeds_with_no_session(self, monkeypatch, tmp_path):
        """CRITICAL: a cache HIT must succeed even when no session is on disk.

        On a cache hit the client is never built, so the session absence
        must not produce an error.
        """
        # Deliberately do NOT save any config or session.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with (
            patch("schwab_cli.commands.history.get_history") as m_api,
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, (
            "Cache HIT must succeed with no session. Got: " + result.output
        )
        m_api.assert_not_called()
        assert "No session" not in result.output
        assert "No config" not in result.output

    def test_cache_hit_succeeds_with_no_config(self, monkeypatch, tmp_path):
        """Same as above but also verifies that 'No config' is not emitted."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with (
            patch("schwab_cli.commands.history.get_history"),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["symbol"] == "AAPL"


# ===========================================================================
# 3. Cache MISS → API → opportunistic backfill
# ===========================================================================


class TestCacheMissApiBackfill:
    """Pin the daily cache-miss → API call → upsert_candles backfill flow."""

    def test_cache_miss_calls_api(self, monkeypatch, tmp_path):
        """When gap() returns non-None, the API must be called."""
        _prep(monkeypatch, tmp_path)
        with (
            patch(
                "schwab_cli.commands.history.get_history", return_value=_RAW
            ) as m_api,
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch(
                "schwab_cli.storage.ohlcv_history.gap",
                return_value=("2024-04-22", "2024-04-23"),
            ),
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_api.assert_called_once()

    def test_cache_miss_daily_upserts_candles(self, monkeypatch, tmp_path):
        """After a daily API call, upsert_candles must be called (backfill)."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history", return_value=_RAW),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch(
                "schwab_cli.storage.ohlcv_history.gap",
                return_value=("2024-04-22", "2024-04-23"),
            ),
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_upsert.assert_called_once()

    def test_cache_miss_upsert_correct_symbol(self, monkeypatch, tmp_path):
        """upsert_candles must be called with the Schwab-canonical symbol."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history", return_value=_RAW),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch(
                "schwab_cli.storage.ohlcv_history.gap",
                return_value=("2024-04-22", "2024-04-23"),
            ),
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert m_upsert.call_args[1]["symbol"] == "AAPL"

    def test_cache_miss_upsert_candle_count(self, monkeypatch, tmp_path):
        """All API candles must be passed to upsert_candles."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history", return_value=_RAW),
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch(
                "schwab_cli.storage.ohlcv_history.gap",
                return_value=("2024-04-22", "2024-04-23"),
            ),
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert len(m_upsert.call_args[1]["candles"]) == 2

    def test_empty_cache_no_db_falls_through_to_api(self, monkeypatch, tmp_path):
        """With empty HOME (no DB), cache path raises and falls through to API."""
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history", return_value=_RAW
        ) as m_api:
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        # The cache is absent (no DB) — _try_cache_response swallows the
        # exception and returns None, so the API must be called.
        assert result.exit_code == 0, result.output
        m_api.assert_called_once()


# ===========================================================================
# 4. Non-daily interval — cache is bypassed entirely
# ===========================================================================


class TestNonDailySkipsCache:
    """Non-daily intervals (minute/weekly/monthly) must never touch the cache."""

    @pytest.mark.parametrize("interval", ["1min", "5min", "15min", "30min"])
    def test_minute_interval_skips_gap_and_read_range(
        self, monkeypatch, tmp_path, interval
    ):
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history", return_value=_RAW),
            patch("schwab_cli.storage.ohlcv_history.gap") as m_gap,
            patch("schwab_cli.storage.ohlcv_history.read_range") as m_read,
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    f"--interval={interval}",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_gap.assert_not_called()
        m_read.assert_not_called()
        m_upsert.assert_not_called()

    def test_weekly_interval_skips_cache(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.history.get_history", return_value=_RAW),
            patch("schwab_cli.storage.ohlcv_history.gap") as m_gap,
            patch("schwab_cli.storage.ohlcv_history.upsert_candles") as m_upsert,
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240101..20240423",
                    "--interval=1wk",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_gap.assert_not_called()
        m_upsert.assert_not_called()

    def test_non_daily_still_calls_api(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history", return_value=_RAW
        ) as m_api:
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=5min",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_api.assert_called_once()


# ===========================================================================
# 5. Empty candles → exit 1
# ===========================================================================


class TestEmptyCandles:
    def test_empty_candles_exit_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        empty = {"symbol": "AAPL", "empty": True, "candles": []}
        with patch("schwab_cli.commands.history.get_history", return_value=empty):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert result.exit_code == 1

    def test_empty_candles_message(self, monkeypatch, tmp_path):
        """The error message must include the symbol and 'No candles found'."""
        _prep(monkeypatch, tmp_path)
        empty = {"symbol": "AAPL", "empty": True, "candles": []}
        with patch("schwab_cli.commands.history.get_history", return_value=empty):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert "No candles found" in result.output
        assert "AAPL" in result.output

    def test_empty_candles_message_includes_range_and_interval(
        self, monkeypatch, tmp_path
    ):
        """The 'No candles found' message must include the original range and
        interval label."""
        _prep(monkeypatch, tmp_path)
        empty = {"symbol": "AAPL", "empty": True, "candles": []}
        with patch("schwab_cli.commands.history.get_history", return_value=empty):
            result = runner.invoke(
                app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
            )
        assert "20240422..20240423" in result.output
        assert "1day" in result.output


# ===========================================================================
# 6. Range errors
# ===========================================================================


class TestRangeErrors:
    def test_invalid_grammar_exit_2(self, monkeypatch, tmp_path):
        """Unparseable range string must exit 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--range=garbage"])
        assert result.exit_code == 2

    def test_invalid_grammar_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--range=garbage"])
        assert "--range must be" in result.output

    def test_ordering_error_exit_1(self, monkeypatch, tmp_path):
        """start >= end must exit 1 (not 2)."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240601..20240101"]
        )
        assert result.exit_code == 1

    def test_ordering_error_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240601..20240101"]
        )
        assert "start must be before end" in result.output

    def test_future_start_exit_1(self, monkeypatch, tmp_path):
        """Future start date must exit 1 (not 2)."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20990101..20990102"]
        )
        assert result.exit_code == 1

    def test_future_start_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20990101..20990102"]
        )
        assert "future" in result.output.lower()

    def test_invalid_date_value_exit_2(self, monkeypatch, tmp_path):
        """A syntactically-valid but semantically-invalid date (e.g., month 13)
        must exit 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--range=20241399..20241401"])
        assert result.exit_code == 2


# ===========================================================================
# 7. Interval and ticker parse errors
# ===========================================================================


class TestParseErrors:
    def test_invalid_interval_exit_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--interval=2min"])
        assert result.exit_code == 2

    def test_invalid_interval_message_lists_valid(self, monkeypatch, tmp_path):
        """Error message must list at least one valid interval."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--interval=2min"])
        assert "1min" in result.output

    def test_bad_ticker_exit_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "NVDA-INVALID-TICKER"])
        assert result.exit_code == 2

    def test_bad_ticker_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "NVDA-INVALID-TICKER"])
        assert "unrecognized ticker" in result.output.lower()

    def test_both_json_md_flags_exit_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--json", "--md"])
        assert result.exit_code == 2

    def test_both_json_md_flags_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["history", "AAPL", "--json", "--md"])
        assert "mutually exclusive" in result.output


# ===========================================================================
# 8. API path — missing config / session / API failures
# ===========================================================================


class TestApiPathErrors:
    def test_no_config_exit_1(self, monkeypatch, tmp_path):
        """No config file must exit 1 on the API path."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
        )
        assert result.exit_code == 1

    def test_no_config_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
        )
        assert "No config" in result.output

    def test_no_session_exit_1(self, monkeypatch, tmp_path):
        """Config present but no session file must exit 1."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
        )
        assert result.exit_code == 1

    def test_no_session_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(
            app, ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"]
        )
        assert "No session" in result.output

    def test_session_expired_exit_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history",
            side_effect=SessionExpired("token expired"),
        ):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"],
            )
        assert result.exit_code == 1

    def test_session_expired_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history",
            side_effect=SessionExpired("token expired"),
        ):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"],
            )
        assert "token expired" in result.output

    def test_api_error_exit_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history",
            side_effect=ApiError("503 Service Unavailable"),
        ):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"],
            )
        assert result.exit_code == 1

    def test_api_error_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.history.get_history",
            side_effect=ApiError("503 Service Unavailable"),
        ):
            result = runner.invoke(
                app,
                ["history", "AAPL", "--range=20240422..20240423", "--interval=1day"],
            )
        assert "503" in result.output

    def test_no_config_not_reached_on_cache_hit(self, monkeypatch, tmp_path):
        """Regression: even when daily API path would fail for no config,
        a cache HIT must still exit 0 and never reach the auth check."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        # Deliberately empty home — no config, no session.
        with (
            patch("schwab_cli.commands.history.get_history") as m_api,
            patch("schwab_cli.storage.vol_history.connect", return_value=_FakeCM()),
            patch("schwab_cli.storage.ohlcv_history.gap", return_value=None),
            patch(
                "schwab_cli.storage.ohlcv_history.read_range",
                return_value=_cache_fake_rows(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "history",
                    "AAPL",
                    "--range=20240422..20240423",
                    "--interval=1day",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        m_api.assert_not_called()
