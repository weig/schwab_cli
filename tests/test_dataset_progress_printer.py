"""The `dataset update` progress printer must survive every event shape.

Regression: the trading-day gate emits a ``skipped_non_trading_day`` event
that carries only ``event`` + ``archive_date`` — no ``index``/``total``/
``symbol``. The printer unconditionally read ``evt["total"]`` and crashed with
KeyError on holidays (Labor Day 2026-09-07 failed the market-data job, exit 1),
recurring every weekday holiday. The printer must handle it as a clean line.
"""
from __future__ import annotations

import pytest

from schwab_cli.commands.dataset import _print_volatility_progress


def test_skipped_non_trading_day_event_does_not_crash():
    # Exactly the dict run_volatility_update emits on a holiday/weekend.
    evt = {"event": "skipped_non_trading_day", "archive_date": "2026-09-07"}
    # Must not raise KeyError('total').
    _print_volatility_progress(evt)


@pytest.mark.parametrize("evt", [
    {"event": "start", "index": 1, "total": 5, "symbol": "AAPL",
     "archive_date": "2026-09-08"},
    {"event": "skipped", "index": 2, "total": 5, "symbol": "TSLA",
     "reason": "FROZEN"},
    {"event": "errored", "index": 3, "total": 5, "symbol": "AVB",
     "error": "400 Bad Request"},
    {"event": "sampled", "index": 4, "total": 5, "symbol": "MSFT",
     "tier_from": "ACTIVE", "tier_to": "ACTIVE"},
])
def test_normal_events_still_print(evt):
    # The full-shape events must keep working unchanged.
    _print_volatility_progress(evt)


def test_any_event_missing_total_is_tolerated():
    """Defense-in-depth: no event shape should ever KeyError the whole job."""
    _print_volatility_progress({"event": "some_future_event"})
