"""Tests for the LiveTicker (background quote poller).

Covers:

* Initial line is always emitted (TTY or not).
* Background polling fires the fetch callback at the configured cadence.
* ``stop()`` joins the thread cleanly.
* No ANSI bytes leak when stderr is not a TTY.
* Fetch exceptions don't crash the ticker.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from schwab_cli.order_pipeline.live_ticker import LiveTicker, TickerConfig


@contextmanager
def _capture_stderr_tty(*, isatty: bool):
    """Replace ``sys.stderr`` with a buffer whose ``isatty()`` is fixed."""
    buf = io.StringIO()
    buf.isatty = lambda: isatty  # type: ignore[method-assign]
    real = sys.stderr
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = real


def test_initial_line_emitted_on_start_non_tty():
    with _capture_stderr_tty(isatty=False) as buf:
        t = LiveTicker(
            fetch=lambda: None,
            render=lambda q: "should-not-appear",
            initial_line="initial",
        )
        t.start()
        t.stop()
    out = buf.getvalue()
    assert "initial" in out
    # No ANSI escapes when stderr is not a TTY.
    assert "\x1b[" not in out


def test_initial_line_emitted_on_start_tty():
    with _capture_stderr_tty(isatty=True) as buf:
        t = LiveTicker(
            fetch=lambda: None,
            render=lambda q: "x",
            initial_line="initial",
            config=TickerConfig(interval_s=10.0),  # don't tick during test
        )
        t.start()
        t.stop()
    assert "initial" in buf.getvalue()


def test_polls_fetch_at_interval():
    """A short interval should produce multiple fetch calls before stop."""
    fetched = []
    barrier = threading.Event()

    def _fetch():
        fetched.append(1)
        if len(fetched) >= 3:
            barrier.set()
        return {"symbol": "X", "last": 1.0}

    with _capture_stderr_tty(isatty=True):
        t = LiveTicker(
            fetch=_fetch,
            render=lambda q: f"tick {q['last']}",
            initial_line="x",
            config=TickerConfig(interval_s=0.05),
        )
        t.start()
        # Wait up to 5s for 3 ticks to land.
        assert barrier.wait(5.0), "ticker did not produce 3 fetches in 5s"
        t.stop()
    assert len(fetched) >= 3


def test_stop_joins_thread_cleanly():
    """After stop(), the worker thread is gone."""
    with _capture_stderr_tty(isatty=True):
        t = LiveTicker(
            fetch=lambda: {"symbol": "X"},
            render=lambda q: "x",
            initial_line="x",
            config=TickerConfig(interval_s=0.05),
        )
        t.start()
        time.sleep(0.1)
        t.stop()
    # Internal thread reference is cleared.
    assert t._thread is None


def test_fetch_exception_does_not_crash_thread():
    calls = []

    def _fetch():
        calls.append(1)
        raise RuntimeError("boom")

    with _capture_stderr_tty(isatty=True):
        t = LiveTicker(
            fetch=_fetch,
            render=lambda q: "x",
            initial_line="x",
            config=TickerConfig(interval_s=0.05),
        )
        t.start()
        time.sleep(0.2)
        t.stop()
    # Thread kept polling despite the exceptions.
    assert len(calls) >= 2


def test_repaint_writes_ansi_only_when_tty():
    """Render output should never appear on non-TTY stderr."""
    fetched = threading.Event()

    def _fetch():
        fetched.set()
        return {"symbol": "X", "last": 99.0}

    with _capture_stderr_tty(isatty=False) as buf:
        t = LiveTicker(
            fetch=_fetch,
            render=lambda q: f"REPAINT {q['last']}",
            initial_line="initial",
            config=TickerConfig(interval_s=0.05),
        )
        t.start()
        # Without ANSI we don't even start the polling thread.
        time.sleep(0.2)
        t.stop()
    out = buf.getvalue()
    assert "initial" in out
    assert "REPAINT" not in out
    assert "\x1b[" not in out
