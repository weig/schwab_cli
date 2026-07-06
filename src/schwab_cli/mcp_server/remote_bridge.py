"""Streamer bridge that forwards to the daemon's shared Schwab stream.

The stdio MCP server must not open its own Schwab WebSocket (Schwab allows
one streamer per account — the daemon owns it). This bridge implements the
same ``add_subscription`` / ``remove_subscription`` / ``close`` surface the
``stream_quote`` tool expects, but each subscription rides the daemon's
shared stream via the existing ``_stream_mcp`` client. If the daemon is
down, the subscription's queue receives an ``{"error": ...}`` update instead
of raising — the tool surfaces it and the rest of the stdio server (REST +
local-DB tools) keeps working.
"""
from __future__ import annotations

import asyncio

from schwab_cli.commands._stream_mcp import (
    DEFAULT_MCP_URL,
    McpUnreachable,
    stream_quotes_via_mcp,
)


class RemoteStreamerBridge:
    """Duck-types :class:`StreamerBridge` for the stdio server."""

    def __init__(self, mcp_url: str = DEFAULT_MCP_URL) -> None:
        self._mcp_url = mcp_url
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    async def add_subscription(
        self, session_id: str, token_key: str, service: str, symbols: list[str]
    ) -> asyncio.Queue:
        """Start forwarding the daemon's updates for ``symbols`` into a queue.

        ``service`` (e.g. LEVELONE_EQUITIES) is the daemon's concern and is
        ignored here — the daemon's ``stream_quote`` handles equities.
        """
        queue: asyncio.Queue = asyncio.Queue()

        async def _pump() -> None:
            try:
                await stream_quotes_via_mcp(
                    list(symbols), mcp_url=self._mcp_url,
                    on_decoded=queue.put_nowait,
                )
            except asyncio.CancelledError:
                raise
            except McpUnreachable:
                queue.put_nowait({"error": "daemon stream unreachable"})
            except Exception as e:  # noqa: BLE001 — surface, never crash the tool
                queue.put_nowait({"error": f"{type(e).__name__}: {e}"})

        self._tasks[(session_id, token_key)] = asyncio.create_task(_pump())
        return queue

    async def remove_subscription(self, session_id: str, token_key: str) -> None:
        task = self._tasks.pop((session_id, token_key), None)
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — cleanup, swallow cancellation
                pass

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
