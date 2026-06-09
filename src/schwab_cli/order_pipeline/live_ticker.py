"""Live underlying-quote ticker for the confirmation prompt.

A small background thread polls ``get_quotes`` for one symbol and
repaints a single status line above the prompt as new data arrives.

Why polling, not the streamer WebSocket?
* A confirmation prompt lives 5-30s — polling at 1-2 Hz is plenty.
* Avoids dragging asyncio + websockets into the sync CLI path.
* No streamer login round-trip; we already have an authed REST client.

The ticker is best-effort: if ``get_quotes`` raises, the tick is
skipped and the next one tries again. Stops cleanly via ``stop_event``.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TickerConfig:
    interval_s: float = 1.5


class StreamQuoteSource:
    """Background daemon-stream feeding a latest-quote snapshot.

    A drop-in data source for :class:`LiveTicker`'s ``fetch``: instead of
    REST-polling the underlying every tick, a background thread subscribes
    to the daemon's *shared* streamer (Schwab allows one streamer per
    account) and keeps the most recent decoded quote. :meth:`latest`
    returns it — ``None`` until the first frame, or if the stream never
    connects — so callers fall back to a REST fetch seamlessly.

    The decoded stream dict and the REST quote share one shape
    (``bid``/``ask``/``last``/``net_change``/sizes/``volume``), so the
    same ``render`` works for either source. Best-effort: any failure
    leaves ``latest()`` at ``None`` and the REST fallback takes over.
    """

    def __init__(self, symbol: str, *, mcp_url: str) -> None:
        self._symbol = symbol.upper()
        self._mcp_url = mcp_url
        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop = None  # the worker thread's event loop
        self._ready = threading.Event()  # set once _loop is usable

    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="ticker-stream", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        # Lazy imports: keep asyncio/websockets out of the sync CLI path
        # unless a daemon ticker is actually started.
        import asyncio

        from schwab_cli.commands._stream_mcp import (
            McpUnreachable,
            stream_quotes_via_mcp,
        )

        def on_decoded(decoded: dict) -> None:
            if (decoded.get("symbol") or "").upper() != self._symbol:
                return
            with self._lock:
                self._latest = decoded

        async def _go() -> None:
            try:
                await stream_quotes_via_mcp(
                    [self._symbol], mcp_url=self._mcp_url, on_decoded=on_decoded,
                )
            except McpUnreachable:
                return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()  # _loop is now safe to use from stop()'s thread
        try:
            loop.run_until_complete(_go())
        except BaseException:  # noqa: BLE001 — incl. CancelledError on stop()
            pass
        finally:
            loop.close()

    def _cancel_all(self) -> None:
        # Runs ON the loop thread (via call_soon_threadsafe) so enumerating
        # and cancelling tasks is race-free; cancelling lets the stream's
        # ``async with`` unwind cleanly (graceful close).
        import asyncio
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    def stop(self) -> None:
        """Cancel the stream and join the thread. Idempotent.

        Waits for the worker's loop to be ready (eliminates a race where a
        very early stop() would miss the cancel and leave the thread
        lingering), then schedules cancellation on the loop thread.
        """
        t = self._thread
        self._thread = None
        if t is None:
            return
        if self._ready.wait(timeout=2.0) and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._cancel_all)
            except RuntimeError:
                pass  # loop already finished/closed
        t.join(timeout=2.0)


def _supports_ansi() -> bool:
    """Only enable cursor-control + line-clear when stderr is a TTY.

    In a CI log or piped stderr, the escape sequences would just become
    noise. Falling back to no live updates is the safe default — the
    prompt still works.
    """
    return getattr(sys.stderr, "isatty", lambda: False)()


class LiveTicker:
    """Background quote poller that repaints one status line.

    Usage::

        ticker = LiveTicker(fetch=lambda: _fetch_underlying_quote_safe(...),
                            render=lambda q: f"📡 Live: {q['last']}",
                            initial_line="(loading…)")
        ticker.start()  # prints initial line + reserves a row above it
        try:
            user_input = sys.stdin.readline()
        finally:
            ticker.stop()

    The thread writes ANSI escape sequences to ``sys.stderr`` to move
    the cursor up one row, clear that row, repaint, and return the
    cursor. When stderr is not a TTY the ticker no-ops on writes.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], dict | None],
        render: Callable[[dict], str],
        initial_line: str,
        config: TickerConfig | None = None,
    ) -> None:
        self._fetch = fetch
        self._render = render
        self._initial_line = initial_line
        self._cfg = config or TickerConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tty = _supports_ansi()

    def start(self) -> None:
        """Print the initial line and start the background poller.

        Layout written by ``start()``::

            <initial_line>            ← row N    (live ticker target)
                                      ← row N+1  (blank separator)
            <prompt>                  ← row N+2  (cursor lands here)

        Each tick the thread moves the cursor up two rows (``\\x1b[2A``)
        to overwrite row N, then restores. The blank separator gives the
        live block visual breathing room from the prompt.
        """
        # Always print the initial line — even on non-TTY — so the user
        # sees the most recent quote at decision time. Trailing "\n\n"
        # creates the blank separator row below the live line.
        sys.stderr.write(self._initial_line + "\n\n")
        sys.stderr.flush()
        if not self._tty:
            return  # no live updates without ANSI; the static line stands
        self._thread = threading.Thread(
            target=self._loop, name="live-ticker", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread and wait briefly for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------

    def _loop(self) -> None:
        # Wait one interval before the first poll — the caller already
        # printed an initial line from the panel-time fetch, so an
        # immediate re-fetch would just produce the same data.
        while not self._stop.wait(self._cfg.interval_s):
            try:
                quote = self._fetch()
            except Exception:  # noqa: BLE001 — best-effort
                continue
            if not quote:
                continue
            line = self._render(quote)
            self._repaint(line)

    def _repaint(self, line: str) -> None:
        """Move cursor up two rows, clear, write ``line``, restore.

        Sequence: ``\\x1b7`` save cursor, ``\\x1b[2A`` up two rows (past
        the blank separator to land on the live row), ``\\x1b[2K\\r``
        clear that line and CR to col 0, ``write(line)``, ``\\x1b8``
        restore cursor (back to where the user is typing).
        """
        if not self._tty:
            return
        try:
            sys.stderr.write("\x1b7\x1b[2A\x1b[2K\r" + line + "\x1b8")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 — never let ticker writes crash
            pass
