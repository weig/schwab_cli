from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

from schwab_cli.api import history as api_history
from schwab_cli.output.history import shape_envelope
from schwab_cli.service import ServiceError
from schwab_cli.service.base import BaseService
from schwab_cli.service.types import HistoryResult
from schwab_cli.storage import ohlcv_history, vol_history

__all__ = ["NoCandles", "HistoryService", "cache_api_response"]


class NoCandles(ServiceError):
    """Raised when the resolved request yields no candles.

    Carries a complete, user-ready message — ``str(e)`` is the full
    sentence, so interfaces can surface it directly.
    """


def _try_cache_response(
    symbol: str, *, start, end,
) -> dict | None:
    """Return a Schwab-shaped response dict when the cache fully
    covers ``[start, end]``, otherwise ``None``.

    Used opportunistically on every daily-interval request — the
    caller falls through to the API on ``None`` and then upserts the
    response back into the cache via :func:`cache_api_response`.
    """
    # The cache buckets by NY trading day. parse_range returns UTC,
    # so convert before lookup or we'd ask for the wrong dates near
    # midnight UTC.
    start_date = start.astimezone(_NY).date()
    end_date   = end.astimezone(_NY).date()
    try:
        with vol_history.connect() as conn:
            if ohlcv_history.gap(
                conn, symbol=symbol, start=start_date, end=end_date,
            ) is not None:
                return None  # cache incomplete — let the API path handle it
            rows = ohlcv_history.read_range(
                conn, symbol=symbol, start=start_date, end=end_date,
            )
    except Exception:
        # If the DB isn't reachable, fall back to the API — better to
        # hit the network than fail the command.
        return None
    return {
        "candles": [
            {"datetime": r["captured_at_ms"],
             "open": r["open"], "high": r["high"],
             "low":  r["low"],  "close": r["close"],
             "volume": r["volume"]}
            for r in rows
        ],
        "symbol": symbol,
    }


def cache_api_response(symbol: str, response: dict) -> None:
    """Best-effort upsert every daily candle from an API response into
    ``ohlcv_daily``. Called after a fallback API fetch so subsequent
    queries within the same range can be served from the cache.

    Failures here are swallowed — caching is a side effect, the user's
    rendered output is the contract.
    """
    try:
        candles = []
        for c in (response.get("candles") or []):
            dt_ms = c.get("datetime")
            if dt_ms is None:
                continue
            day = (
                datetime.fromtimestamp(int(dt_ms) / 1000, tz=timezone.utc)
                        .astimezone(_NY).date().isoformat()
            )
            candles.append({
                "day": day,
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume") or 0),
                "captured_at_ms": int(dt_ms),
            })
        if not candles:
            return
        with vol_history.connect() as conn:
            ohlcv_history.upsert_candles(
                conn, symbol=symbol, candles=candles,
            )
    except Exception:
        pass  # opportunistic — never break the user's command


class HistoryService(BaseService):
    """Layer-2 service for the ``history`` command."""

    def get_history(
        self,
        symbol: str,
        *,
        frequency_type: str,
        frequency: int,
        label: str,
        start: datetime,
        end: datetime,
        range_str: str,
    ) -> HistoryResult:
        """Owns the cache + auth + API orchestration for the ``history`` command.

        For daily intervals, tries the opportunistic OHLCV cache first; a full
        cache HIT returns without ever loading config/session or building the
        HTTP client. On a cache miss (or non-daily interval), loads config and
        session, fetches from Schwab, and opportunistically backfills the cache
        for daily intervals.

        Raises :class:`schwab_cli.service.auth.NotConfigured` when no config is
        on disk, the auth exceptions from :mod:`schwab_cli.service.auth` when the
        session is missing/expired, and :class:`NoCandles` when the shaped
        envelope has no candles. ``(ApiError, SessionExpired)`` from the API call
        propagate unchanged.
        """
        # Cache-first read for daily intervals — regardless of whether
        # the symbol is in the ohlcv subscription group. The cache is
        # opportunistically populated by every API fallback below, so
        # frequent `history` queries naturally build up a local store.
        # Non-daily intervals (1min/5min/1wk/1mo) skip the cache entirely
        # — ``ohlcv_daily`` only stores daily candles.
        raw: dict | None = None
        is_daily = frequency_type == "daily"
        if is_daily:
            raw = _try_cache_response(symbol, start=start, end=end)

        if raw is None:
            with self._authed_client() as client:
                raw = api_history.get_history(
                    client,
                    symbol,
                    frequency_type=frequency_type,
                    frequency=frequency,
                    start=start,
                    end=end,
                )
            # Opportunistic backfill: every daily-interval API response
            # seeds the cache. Subsequent queries within this range
            # (including for un-subscribed symbols) skip the network.
            if is_daily:
                cache_api_response(symbol, raw)

        envelope = shape_envelope(raw, interval=label)
        if not envelope["candles"]:
            raise NoCandles(
                f"No candles found for {symbol} in {range_str} at {label}."
            )

        return HistoryResult(envelope=envelope)
