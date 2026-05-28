"""Structured event logger for the MCP server.

Every subscribe / unsubscribe / auth / guardrail / error event goes
through :meth:`LogBook.emit` and is written as one JSON line to
both stderr (for foreground runs) and, optionally, a rolling log
file (so ``schwab server log -f`` can tail from another terminal).

Event schema is informal — callers pass an ``event`` string and any
keyword fields, which are serialised as top-level JSON keys alongside
``ts`` / ``level`` / ``event``. See ``docs/plan/mcp-streaming.md`` for
the catalog the MCP server emits in practice.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


class LogBook:
    """Dual-sink structured logger.

    Writes to an in-memory stream (stderr by default) plus an optional
    append-mode file. Both are flushed on every emit so tail-followers
    see entries within one event of them happening.
    """

    def __init__(
        self,
        *,
        log_file: Path | None = None,
        stream: TextIO | None = None,
        clock=None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._log_file = log_file
        # Allow tests to inject a deterministic timestamp.
        self._clock = clock if clock is not None else _default_clock
        if self._log_file is not None:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        **fields: object,
    ) -> None:
        """Write one structured entry to stderr + the log file.

        Extra kwargs become top-level JSON keys. Exceptions during
        writing (e.g. disk full) are suppressed — the server must
        not die because logging failed."""
        entry: dict[str, object] = {
            "ts": self._clock(),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(entry, default=str)
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            pass
        if self._log_file is not None:
            try:
                with self._log_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def info(self, event: str, **fields: object) -> None:
        self.emit(event, level="info", **fields)

    def warning(self, event: str, **fields: object) -> None:
        self.emit(event, level="warning", **fields)

    def error(self, event: str, **fields: object) -> None:
        self.emit(event, level="error", **fields)


def _default_clock() -> str:
    """ISO 8601 UTC timestamp with millisecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
