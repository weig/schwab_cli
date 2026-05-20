"""Inbound Telegram — long-polling ``getUpdates`` client.

Phase 2d foundation: lets the CLI block waiting for a reply from the
authorised chat (e.g. ``CONFIRM_OVERRIDE`` during a Phase 2e
``telegram_inbound`` override). Send-side stays in
:mod:`schwab_cli.notify.telegram`; this module is receive-only.

Design notes:

* **Long polling.** ``getUpdates`` blocks server-side for up to
  ``poll_timeout`` seconds; we cap at 30s so a Ctrl+C feels snappy.
* **Drain + watermark.** :meth:`TelegramPoller.drain` flushes any
  pending updates and records the next ``update_id`` baseline so we
  never see stale messages from before the wait started.
* **Allowlist.** Only messages from ``chat_id`` are surfaced;
  optional ``allowed_user_ids`` provides an extra filter for groups.
* **No persistence.** Each ``wait_for_reply`` call is independent —
  the watermark lives in memory only. Phase 2e starts a fresh poller
  every override.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Callable

import httpx


_POLL_TIMEOUT = 30                  # seconds — upper-bound long-poll
_HTTP_TIMEOUT = _POLL_TIMEOUT + 10  # client-side timeout > server's


@dataclass
class TelegramPoller:
    """Stateful poller for one bot + one allowlisted chat.

    ``allowed_user_ids`` is an optional secondary filter — when set,
    a message is only delivered if the sender's user-id is in the
    set. Useful when the chat is a group that a few people share
    but we only want our own messages to count.
    """

    bot_token: str
    chat_id: str                              # the authorised chat (string for /channel id)
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    api_base: str = "https://api.telegram.org"
    _last_update_id: int = -1                 # -1 → not yet drained

    @property
    def url(self) -> str:
        return f"{self.api_base}/bot{self.bot_token}"

    async def drain(self, *, http: httpx.AsyncClient | None = None) -> None:
        """Skip any pending updates so the next ``poll`` only sees
        messages that arrive after this call.

        Telegram delivers updates in id-order; once we acknowledge an
        id by passing ``offset=id+1``, the server drops earlier ones.
        We use that to fast-forward to the latest pending id.
        """
        client_owned = http is None
        client = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        try:
            r = await client.get(
                f"{self.url}/getUpdates",
                params={"offset": -1, "timeout": 0, "limit": 1},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("ok") and data.get("result"):
                last = data["result"][-1]
                self._last_update_id = int(last.get("update_id", -1))
            else:
                self._last_update_id = 0
        finally:
            if client_owned:
                await client.aclose()

    async def poll_once(
        self, *, http: httpx.AsyncClient,
        poll_timeout: int = _POLL_TIMEOUT,
    ) -> list[dict]:
        """Single ``getUpdates`` round-trip. Returns the raw updates
        list (already-acked = forgotten by Telegram).

        Caller must have called :meth:`drain` (or set
        ``_last_update_id`` directly) so the offset is right.
        """
        offset = (self._last_update_id + 1) if self._last_update_id >= 0 else 0
        r = await http.get(
            f"{self.url}/getUpdates",
            params={"offset": offset, "timeout": poll_timeout, "limit": 50},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return []
        updates = data.get("result") or []
        if updates:
            self._last_update_id = int(updates[-1].get("update_id", offset))
        return updates

    async def wait_for_reply(
        self,
        predicate: Callable[[dict], bool],
        *,
        timeout_seconds: int,
        http: httpx.AsyncClient | None = None,
    ) -> dict | None:
        """Long-poll until a message satisfies ``predicate`` or until
        ``timeout_seconds`` elapses.

        ``predicate`` is called with the raw message dict (the
        ``message`` key under one update). Returns the matched
        message dict, or ``None`` on timeout.

        Network errors are retried with backoff up to the deadline —
        a transient blip shouldn't kill an override flow.
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        client_owned = http is None
        client = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        try:
            backoff = 1.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                # Cap the per-call long-poll at remaining time.
                this_timeout = min(_POLL_TIMEOUT, max(1, int(remaining)))
                try:
                    updates = await self.poll_once(
                        http=client, poll_timeout=this_timeout,
                    )
                    backoff = 1.0
                except (httpx.RequestError, json.JSONDecodeError):
                    # Retry with capped exponential backoff.
                    await asyncio.sleep(min(backoff, remaining))
                    backoff = min(backoff * 2, 8.0)
                    continue

                for upd in updates:
                    msg = upd.get("message") or upd.get("edited_message")
                    if not isinstance(msg, dict):
                        continue
                    if not self._allowed(msg):
                        continue
                    if predicate(msg):
                        return msg
                # Loop again — long-poll already burned the wait.
        finally:
            if client_owned:
                await client.aclose()

    def _allowed(self, msg: dict) -> bool:
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != self.chat_id:
            return False
        if self.allowed_user_ids:
            from_user = msg.get("from") or {}
            if from_user.get("id") not in self.allowed_user_ids:
                return False
        return True


# ---- sync wrapper for the CLI override flow ------------------------------


def wait_for_text_reply(
    *,
    bot_token: str,
    chat_id: str,
    expected_text: str,
    timeout_seconds: int = 300,
    case_sensitive: bool = True,
    allowed_user_ids: frozenset[int] = frozenset(),
) -> str | None:
    """Block until a reply with text equal to ``expected_text``
    arrives in ``chat_id``, or until the timeout elapses.

    Returns the matched message text on success, ``None`` on timeout.

    This is the synchronous front door Phase 2e's override flow uses;
    it spins up a one-shot asyncio loop and a single
    :class:`TelegramPoller`.
    """
    poller = TelegramPoller(
        bot_token=bot_token, chat_id=chat_id,
        allowed_user_ids=allowed_user_ids,
    )

    if case_sensitive:
        def matches(msg: dict) -> bool:
            return (msg.get("text") or "") == expected_text
    else:
        target = expected_text.lower()
        def matches(msg: dict) -> bool:
            return (msg.get("text") or "").lower() == target

    async def _run() -> str | None:
        await poller.drain()
        msg = await poller.wait_for_reply(
            matches, timeout_seconds=timeout_seconds,
        )
        return (msg or {}).get("text") if msg else None

    return asyncio.run(_run())


# ---- helper: load chat-id allowlist from Claude channel config ---------


def load_claude_allowlist() -> frozenset[int]:
    """Read ``~/.claude/channels/telegram/access.json`` if present
    and return ``allowFrom`` as ``int`` user-ids.

    Returns an empty set if the file is missing or malformed —
    callers treat that as "no extra user filter" (rely on chat_id
    membership only).
    """
    from pathlib import Path
    path = Path.home() / ".claude" / "channels" / "telegram" / "access.json"
    if not path.exists():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return frozenset()
    raw = data.get("allowFrom") or []
    out: set[int] = set()
    for v in raw:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return frozenset(out)
