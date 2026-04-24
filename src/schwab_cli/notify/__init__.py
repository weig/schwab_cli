"""Notification dispatch for the MCP daemon.

:class:`Notifier` is the single entrypoint the rest of the code
calls. It routes each event to every configured channel whose
subscription list includes the event name, enforces per-channel
per-event rate limiting (default 5 min), and swallows transport
errors so a broken Telegram bot can never take down the server.

Usage:

    notifier = Notifier.from_file()           # loads notification.json
    notifier.emit("auth.auto_login.failed", stderr_tail="...")

Integration with the logbook: every emitted notification — plus
every channel failure — is logged as a structured event by the
caller. Callers are expected to pass a :class:`LogBook` at
construction time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.notify import config as notify_config
from schwab_cli.notify import telegram as telegram_channel
from schwab_cli.notify.events import EVENTS, level_of, summary_of


@dataclass
class _RateLimitKey:
    channel: str
    event: str


class Notifier:
    """Dispatches events to configured channels with rate-limit guards."""

    def __init__(
        self,
        cfg: notify_config.NotificationConfig,
        logbook: LogBook | None = None,
        *,
        clock=None,
    ) -> None:
        self._cfg = cfg
        self._logbook = logbook
        self._clock = clock if clock is not None else time.time
        # Last-send timestamp per (channel, event) pair for rate
        # limiting. Starts empty — first emit in each window
        # always goes through.
        self._last_sent: dict[tuple[str, str], float] = {}

    # ---- factories -----------------------------------------------------

    @classmethod
    def from_file(
        cls,
        path: Path | None = None,
        logbook: LogBook | None = None,
    ) -> "Notifier":
        """Load ``notification.json`` from ``path`` (or the default
        location) and return a ready-to-use notifier. Missing file
        yields an inert notifier — ``emit()`` is a no-op."""
        return cls(notify_config.load(path), logbook=logbook)

    # ---- dispatch ------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> None:
        """Fire an event. Safe to call even when no channels are
        configured — the method silently returns. Rate-limited
        per (channel, event) pair.

        Channel-specific transport errors are caught and logged
        at ``warning`` level via the notifier's logbook (if any).
        """
        level = level_of(event)
        summary = summary_of(event)

        # Telegram.
        tg = self._cfg.telegram
        if tg.configured and event in tg.events:
            self._dispatch_telegram(tg, event, level, summary, fields)

    def _dispatch_telegram(
        self,
        tg: notify_config.TelegramSettings,
        event: str,
        level: str,
        summary: str,
        fields: dict[str, Any],
    ) -> None:
        if self._rate_limited("telegram", event, tg.rate_limit_seconds):
            if self._logbook is not None:
                # LogBook.info's positional is `event`; rename the
                # payload key to avoid the kwarg collision.
                self._logbook.info(
                    "notify.rate_limited",
                    channel="telegram", target_event=event,
                )
            return
        text = telegram_channel.format_message(event, level, summary, fields)
        ok, detail = telegram_channel.send(
            bot_token=tg.bot_token,  # type: ignore[arg-type]
            chat_id=tg.chat_id,      # type: ignore[arg-type]
            text=text,
        )
        if ok:
            self._last_sent[("telegram", event)] = self._clock()
            if self._logbook is not None:
                self._logbook.info(
                    "notify.sent",
                    channel="telegram",
                    target_event=event,
                    target_level=level,
                )
        else:
            if self._logbook is not None:
                self._logbook.warning(
                    "notify.send_failed",
                    channel="telegram", target_event=event, detail=detail,
                )

    # ---- rate limiting -------------------------------------------------

    def _rate_limited(self, channel: str, event: str, window: int) -> bool:
        last = self._last_sent.get((channel, event))
        if last is None:
            return False
        return (self._clock() - last) < window

    # ---- introspection -------------------------------------------------

    @property
    def config(self) -> notify_config.NotificationConfig:
        return self._cfg

    def channels_summary(self) -> dict[str, dict[str, Any]]:
        """For ``schwab_cli notify list``: what's configured + the
        subscribed events per channel, without secrets."""
        tg = self._cfg.telegram
        return {
            "telegram": {
                "configured": tg.configured,
                "events": list(tg.events),
                "rate_limit_seconds": tg.rate_limit_seconds,
            },
            "slack": {
                "configured": False,
                "note": "tbd (Phase 2b)",
            },
        }


__all__ = [
    "Notifier",
    "EVENTS",
]
