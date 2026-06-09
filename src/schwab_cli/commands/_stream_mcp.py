"""Shared helpers for streaming Schwab quotes through the MCP daemon.

Both ``schwab stream`` and ``schwab watch`` prefer the daemon's single
shared Schwab WebSocket (Schwab allows only one streamer per account) and
fall back to opening their own connection only when no daemon is running.
This module holds the pieces they share: a cheap reachability probe and a
generic ``stream_quote`` consumer that hands each decoded quote update to
a caller-supplied callback (line renderer for ``stream``, table updater
for ``watch``).
"""
from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


DEFAULT_MCP_URL = "http://127.0.0.1:7234/mcp"


class McpUnreachable(Exception):
    """The MCP daemon TCP-accepts but the JSON-RPC stream fails — the
    caller may fall back to a direct Schwab connection."""


def probe_daemon(mcp_url: str) -> bool:
    """Cheap TCP probe of the daemon's HTTP port. Doesn't do a full HTTP
    request — just confirms something is listening, enough to decide
    whether to attempt the MCP client handshake."""
    parsed = urlparse(mcp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7234
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


async def stream_quotes_via_mcp(
    symbols: list[str],
    *,
    mcp_url: str,
    on_decoded: Callable[[dict[str, Any]], None],
) -> None:
    """Connect to a running MCP daemon, subscribe via the ``stream_quote``
    tool, and invoke ``on_decoded`` with each decoded quote update.

    Progress notifications are delivered through ``ClientSession``'s
    ``progress_callback`` — passing it makes the SDK auto-generate a
    ``progressToken`` and route matching notifications to the callback.
    The call returns only on cancellation (Ctrl+C) or error. Raises
    :class:`McpUnreachable` if the connection drops so the caller can
    fall back to a direct connection.

    Non-JSON progress messages (and the ``keepalive`` sentinel) are
    dropped — every real quote update arrives as a JSON object. Callers
    that need raw text should not use this helper.
    """
    # Deferred imports — direct-mode callers shouldn't pay the MCP client
    # import cost.
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    http_url = mcp_url.rstrip("/")
    if not http_url.endswith("/mcp"):
        http_url = http_url + "/mcp"

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        if message is None or message == "keepalive":
            return
        try:
            decoded = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(decoded, dict):
            on_decoded(decoded)

    try:
        async with streamable_http_client(http_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "stream_quote",
                    arguments={"symbols": [s.upper() for s in symbols]},
                    progress_callback=on_progress,
                )
    except (ConnectionError, OSError) as e:
        raise McpUnreachable(str(e)) from e
    except KeyboardInterrupt:
        return
