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
