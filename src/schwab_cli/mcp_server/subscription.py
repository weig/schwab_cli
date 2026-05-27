"""Refcounted subscription manager for the MCP server.

Bridges many-to-one between N connected MCP clients and one shared
Schwab streamer WebSocket:

* **Deduplicates** at the Schwab side — if three agents subscribe
  to ``NVDA``, we SUBS once.
* **Fans out** at the MCP side — each incoming Schwab data frame
  is delivered to every ``(session, progress_token)`` pair that
  currently wants it.

All state lives in three dicts (see ``__init__``); every operation
is O(1) or O(keys) per call. Pure logic, no I/O — callers are
responsible for sending the returned SUBS/UNSUBS commands to
Schwab and forwarding data frames via the fan-out list.

Concurrency note: this class is intended to run inside a single
asyncio event loop. Callers that need true thread-safety should
wrap mutations in an ``asyncio.Lock`` — but for the single-loop
server design we ship, that's unnecessary.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class SubKey:
    """Canonical subscription key — service + symbol.

    Using a dataclass rather than a tuple so call sites read as
    ``sub.service`` / ``sub.symbol`` instead of positional
    indexing, and so ``set[SubKey]`` type-annotates cleanly.
    """

    service: str
    symbol: str


class SubscriptionManager:
    """Central routing + refcount table for the MCP server.

    Methods return the *delta* the caller needs to apply to Schwab
    — ``add()`` returns keys to ``SUBS``, ``remove()`` /
    ``drop_session()`` return keys to ``UNSUBS``. Caller is free to
    batch them into one Schwab frame per service.
    """

    def __init__(self) -> None:
        # Global refcount per subscription key.
        self._refcount: dict[SubKey, int] = defaultdict(int)
        # Symbol → which (session, token) pairs are subscribed.
        self._routing: dict[SubKey, set[tuple[str, str]]] = defaultdict(set)
        # Session → progress_token → subscription keys.
        self._session_subs: dict[str, dict[str, set[SubKey]]] = defaultdict(
            lambda: defaultdict(set)
        )

    # ---- mutation ------------------------------------------------------

    def add(
        self,
        session: str,
        progress_token: str,
        service: str,
        symbols: list[str],
    ) -> set[SubKey]:
        """Register a new subscription from one MCP client.

        Returns the set of subscription keys whose refcount
        transitioned 0 → 1 on this add — the caller must send
        ``SUBS`` for each to Schwab. Keys already at refcount ≥ 1
        are not returned.

        Passing an empty ``symbols`` list is a no-op (no session
        registration, no refcount change).
        """
        if not symbols:
            return set()

        newly_subscribed: set[SubKey] = set()
        for sym in symbols:
            key = SubKey(service, sym)
            if self._refcount[key] == 0:
                newly_subscribed.add(key)
            self._refcount[key] += 1
            self._routing[key].add((session, progress_token))
            self._session_subs[session][progress_token].add(key)
        return newly_subscribed

    def remove(
        self,
        session: str,
        progress_token: str,
    ) -> set[SubKey]:
        """Remove all subscriptions a single tool call held.

        Returns the set of keys whose refcount transitioned
        1 → 0 — the caller must ``UNSUBS`` each from Schwab.
        """
        sess = self._session_subs.get(session)
        if not sess:
            return set()
        keys = sess.pop(progress_token, set())
        if not sess:
            # Clean up the empty session entry.
            self._session_subs.pop(session, None)
        return self._decrement_keys(keys, session, progress_token)

    def drop_session(self, session: str) -> set[SubKey]:
        """Remove every subscription this session holds (TCP close,
        idle timeout, etc.). Returns keys to ``UNSUBS``."""
        sess = self._session_subs.pop(session, None)
        if not sess:
            return set()
        unsubscribed: set[SubKey] = set()
        for progress_token, keys in sess.items():
            unsubscribed |= self._decrement_keys(keys, session, progress_token)
        return unsubscribed

    def _decrement_keys(
        self,
        keys: set[SubKey],
        session: str,
        progress_token: str,
    ) -> set[SubKey]:
        unsubscribed: set[SubKey] = set()
        for key in keys:
            count = self._refcount.get(key, 0)
            if count <= 1:
                unsubscribed.add(key)
                self._refcount.pop(key, None)
                self._routing.pop(key, None)
            else:
                self._refcount[key] = count - 1
                self._routing[key].discard((session, progress_token))
        return unsubscribed

    # ---- read ----------------------------------------------------------

    def fanout_targets(
        self, service: str, symbol: str
    ) -> list[tuple[str, str]]:
        """Return ``(session, progress_token)`` pairs currently
        subscribed to this service + symbol. Caller iterates and
        delivers the data frame to each via MCP progress
        notifications.

        Order is insertion-order-preserving for determinism in
        tests; the set underneath is copied into a sorted list.
        """
        key = SubKey(service, symbol)
        return sorted(self._routing.get(key, set()))

    def active_symbols(self) -> set[SubKey]:
        """All keys currently with refcount ≥ 1. Used by the
        reconnect path to rebuild the Schwab subscription set
        after a dropped WebSocket."""
        return set(self._refcount)

    # ---- introspection (for `server status`) --------------------------

    def snapshot(self) -> dict[str, object]:
        """Admin-endpoint-friendly state snapshot.

        Shape is stable for consumers — ``server status`` relies on
        these exact keys.
        """
        sessions = {}
        for sess, tokens in self._session_subs.items():
            per_sess_symbols: set[str] = set()
            for keys in tokens.values():
                per_sess_symbols |= {k.symbol for k in keys}
            sessions[sess] = {
                "symbols": sorted(per_sess_symbols),
                "progress_stream_count": len(tokens),
            }

        subscriptions = []
        for key, refcount in self._refcount.items():
            sess_ids = sorted({s for (s, _) in self._routing.get(key, set())})
            subscriptions.append({
                "service": key.service,
                "symbol": key.symbol,
                "refcount": refcount,
                "sessions": sess_ids,
            })
        subscriptions.sort(key=lambda s: (s["service"], s["symbol"]))

        return {
            "session_count": len(sessions),
            "subscription_count": len(subscriptions),
            "sessions": sessions,
            "subscriptions": subscriptions,
        }
