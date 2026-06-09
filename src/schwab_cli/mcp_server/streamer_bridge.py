"""Streamer ⇄ MCP bridge.

One shared Schwab streamer WebSocket feeding N per-subscriber async
queues, routed through :class:`SubscriptionManager`. Owns the
streamer lifecycle:

* Opens on first subscription (lazy connect + login).
* Closes on last subscription removal (no work to keep it for).
* Reconnects on transient failure (TODO — Phase 3).

All subscribe / unsubscribe / Schwab-wire events emit structured log
entries via the injected :class:`LogBook` so ``schwab server log
-f`` and `server status` can observe the daemon's behavior.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.streamer import (
    Streamer,
    StreamerInfo,
    classify_frame,
    fetch_streamer_info,
    is_heartbeat,
)
from schwab_cli.api.streamer_fields import decode, default_fields
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.mcp_server.subscription import SubscriptionManager, SubKey


class StreamerBridge:
    """Glue between :class:`SubscriptionManager` and a single
    Schwab :class:`Streamer`. Shared across every MCP session."""

    def __init__(
        self,
        client: SchwabClient,
        logbook: LogBook,
        manager: SubscriptionManager,
        *,
        notifier=None,
        idle_linger_s: float = 45.0,
    ) -> None:
        self._client = client
        self._logbook = logbook
        self._manager = manager
        self._notifier = notifier
        self._streamer: Streamer | None = None
        self._reader_task: asyncio.Task | None = None
        self._queues: dict[tuple[str, str], asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count = 0
        # When the subscription refcount hits zero we keep the socket open
        # for ``idle_linger_s`` so a re-subscribe within the window reuses
        # it (avoids a ~1s reconnect). 0 = close immediately (legacy).
        self._idle_linger_s = idle_linger_s
        self._idle_close_task: asyncio.Task | None = None
        # streamer_info (socket URL + IDs from /userPreference) is static
        # for the daemon's life — cache it so post-linger / post-rotation
        # re-opens skip the REST round-trip. Cleared on rotation as cheap
        # insurance against stale socket metadata.
        self._cached_info: StreamerInfo | None = None

    # ---- state ---------------------------------------------------------

    def streamer_state(self) -> str:
        if self._streamer is None:
            return "idle"
        if self._idle_close_task is not None and not self._idle_close_task.done():
            return "lingering"
        return "connected"

    def reconnect_count(self) -> int:
        return self._reconnect_count

    # ---- subscription lifecycle ---------------------------------------

    async def add_subscription(
        self,
        session: str,
        progress_token: str,
        service: str,
        symbols: list[str],
    ) -> asyncio.Queue:
        """Register a subscription from one MCP tool call.

        Returns the queue the caller should pump to progress
        notifications. The queue is bounded; full queues drop
        updates and log a warning (agents that can't keep up
        shouldn't starve others).
        """
        async with self._lock:
            # Cancel any pending idle-close: a subscriber is back within
            # the linger window, so the live socket is reused as-is.
            self._cancel_idle_close()
            await self._ensure_connected()
            new_keys = self._manager.add(session, progress_token, service, symbols)
            queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
            self._queues[(session, progress_token)] = queue
            self._logbook.info(
                "subscribe",
                session=session,
                progress_token=progress_token,
                service=service,
                symbols=list(symbols),
                new_to_schwab=[k.symbol for k in new_keys],
            )
            if new_keys and self._streamer is not None:
                await self._schwab_subscribe(new_keys)
            return queue

    async def remove_subscription(
        self,
        session: str,
        progress_token: str,
    ) -> None:
        async with self._lock:
            gone_keys = self._manager.remove(session, progress_token)
            self._queues.pop((session, progress_token), None)
            self._logbook.info(
                "unsubscribe",
                session=session,
                progress_token=progress_token,
                reason="cancelled",
                unsubs_at_schwab=[k.symbol for k in gone_keys],
            )
            if gone_keys and self._streamer is not None:
                await self._schwab_unsubscribe(gone_keys)
            if not self._manager.active_symbols():
                await self._on_refcount_zero()

    async def drop_session(self, session: str) -> None:
        """Clean up every subscription a session held — called on
        MCP client TCP close / idle timeout."""
        async with self._lock:
            gone_keys = self._manager.drop_session(session)
            stale = [k for k in self._queues if k[0] == session]
            for k in stale:
                self._queues.pop(k, None)
            self._logbook.info(
                "session.drop",
                session=session,
                unsubs_at_schwab=[k.symbol for k in gone_keys],
            )
            if gone_keys and self._streamer is not None:
                await self._schwab_unsubscribe(gone_keys)
            if not self._manager.active_symbols():
                await self._on_refcount_zero()

    # ---- Schwab wire ops ----------------------------------------------

    async def _schwab_subscribe(self, keys: set[SubKey]) -> None:
        by_service: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            by_service[k.service].append(k.symbol)
        assert self._streamer is not None
        for svc, syms in by_service.items():
            await self._streamer.subscribe(
                service=svc, keys=syms, fields=default_fields(svc),
            )
            self._logbook.info(
                "schwab.subs", service=svc, symbols=syms,
            )

    async def _schwab_unsubscribe(self, keys: set[SubKey]) -> None:
        by_service: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            by_service[k.service].append(k.symbol)
        assert self._streamer is not None
        for svc, syms in by_service.items():
            try:
                await self._streamer.unsubscribe(service=svc, keys=syms)
                self._logbook.info(
                    "schwab.unsubs", service=svc, symbols=syms,
                )
            except Exception as e:
                self._logbook.warning(
                    "schwab.unsubs_failed",
                    service=svc, symbols=syms, error=str(e),
                )

    # ---- reconnect on token rotation ----------------------------------

    async def reconnect_after_rotation(self) -> None:
        """Close the current Schwab streamer and re-subscribe every
        active symbol against a freshly-refreshed access token.

        Called by the auth monitor's rotation-success hook. Existing
        subscriber queues are preserved — they continue receiving
        data once the new streamer connection comes back up.
        """
        async with self._lock:
            active = self._manager.active_symbols()
            await self._close_streamer()
            # Refetch streamer_info on the next connect — don't run a
            # long-lived daemon on socket metadata cached before rotation.
            self._cached_info = None
            if not active:
                # Nothing to resubscribe; the next add_subscription
                # call will reopen normally.
                return
            await self._ensure_connected()
            # Send SUBS for everything still in the refcount table.
            await self._schwab_subscribe(active)
            self._logbook.info(
                "streamer.resubscribed_after_rotation",
                symbol_count=len(active),
            )

    # ---- idle linger --------------------------------------------------

    async def _on_refcount_zero(self) -> None:
        """The last subscription just went away (called under the lock).

        With ``idle_linger_s <= 0`` close immediately (legacy). Otherwise
        arm a single cancellable timer that closes the socket once the
        linger elapses *if still idle* — so a re-subscribe within the
        window reuses the live connection instead of reconnecting.
        """
        if self._idle_linger_s <= 0:
            await self._close_streamer()
            return
        self._cancel_idle_close()
        self._idle_close_task = asyncio.create_task(self._idle_close_after())
        self._logbook.info(
            "streamer.lingering", linger_s=self._idle_linger_s,
        )

    async def _idle_close_after(self) -> None:
        try:
            await asyncio.sleep(self._idle_linger_s)
        except asyncio.CancelledError:
            return
        async with self._lock:
            # Clear our own handle first so _close_streamer's defensive
            # _cancel_idle_close() doesn't try to cancel the task we're
            # running in. Re-check the refcount: a subscriber may have
            # arrived after the sleep but before we took the lock.
            self._idle_close_task = None
            if not self._manager.active_symbols():
                await self._close_streamer()

    def _cancel_idle_close(self) -> None:
        task = self._idle_close_task
        self._idle_close_task = None
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Daemon-shutdown teardown. Cancels any pending idle-close timer
        and closes the socket so no task is left pending when the event
        loop tears down. Idempotent."""
        async with self._lock:
            await self._close_streamer()

    # ---- connection lifecycle -----------------------------------------

    async def _ensure_connected(self) -> None:
        if self._streamer is not None:
            return
        if self._cached_info is None:
            self._cached_info = fetch_streamer_info(self._client)
        info = self._cached_info
        streamer = Streamer(info, self._client.session.access_token)
        await streamer.connect()
        await streamer.login()
        self._streamer = streamer
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._logbook.info(
            "streamer.connect",
            url=info.socket_url,
            customer_id=info.customer_id,
        )

    async def _close_streamer(self) -> None:
        # Forced closes (rotation, shutdown) must also drop any pending
        # idle-close timer. No-op when called from _idle_close_after,
        # which clears the handle before invoking us.
        self._cancel_idle_close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._streamer is not None:
            try:
                await self._streamer.close()
            finally:
                self._streamer = None
                self._logbook.info("streamer.disconnect")

    # ---- fan-out loop --------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read every frame from Schwab, decode data frames, fan out
        to subscribers' queues. Exits when the WebSocket closes."""
        assert self._streamer is not None
        try:
            async for frame in self._streamer.messages():
                if is_heartbeat(frame):
                    continue
                kind = classify_frame(frame)
                if kind == "response":
                    # Log SUBS/UNSUBS acks at debug; keep noise down
                    # at info level by not logging every one.
                    continue
                if kind != "data":
                    continue
                for chunk in frame.get("data") or []:
                    service = chunk.get("service") or ""
                    for content in chunk.get("content") or []:
                        decoded = decode(service, content)
                        sym = decoded.get("symbol")
                        if not sym:
                            continue
                        self._dispatch(service, sym, decoded)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logbook.error("streamer.reader_error", error=str(e))
            if self._notifier is not None:
                try:
                    self._notifier.emit(
                        "streamer.crash",
                        error=f"{type(e).__name__}: {e}",
                    )
                except Exception:
                    pass

    def _dispatch(
        self, service: str, symbol: str, update: dict[str, Any]
    ) -> None:
        for target in self._manager.fanout_targets(service, symbol):
            q = self._queues.get(target)
            if q is None:
                continue
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                self._logbook.warning(
                    "queue.full",
                    session=target[0],
                    progress_token=target[1],
                    symbol=symbol,
                )
