"""``schwab mcp --stdio`` — transparent stdio↔HTTP bridge to the daemon.

Claude Desktop (and any MCP client whose config only speaks stdio) can't
dial the daemon's Streamable-HTTP ``/mcp`` endpoint directly. This command
is a dumb pipe: it talks stdio to the client and forwards every JSON-RPC
frame, untouched, to the single always-on ``schwab server`` daemon — the
sole Schwab token owner and the sole Schwab streamer.

Crucially it is **not** a second MCP server. It starts no server, holds no
credentials, and opens no Schwab connection of its own. A standalone stdio
MCP server would become a second token owner (racing the daemon on refresh)
and a second streamer (Schwab allows only one connection per account) — the
exact failure modes the daemon architecture exists to avoid. This bridge
plays the same role as the node tool ``mcp-remote``, minus the node
dependency, reusing the ``schwab`` binary the user already has installed.
"""

from __future__ import annotations

import sys

import anyio

from schwab_cli.auth_delegate import daemon_url
from schwab_cli.commands._stream_mcp import probe_daemon


def _mcp_endpoint() -> str:
    """Streamable-HTTP MCP endpoint of the running daemon.

    Reuses :func:`daemon_url` (which honours ``SCHWAB_DAEMON_URL``) so the
    bridge always targets the same daemon the rest of the CLI does.
    """
    return daemon_url() + "/mcp"


async def _pump(src, dst, cancel) -> None:
    """Forward every message from ``src`` to ``dst`` until either end ends.

    Read streams may yield ``Exception`` items (transport-level parse
    errors); those can't be forwarded to a sink that only accepts messages,
    so they're dropped and the pipe stays open. When the source is exhausted
    or a stream breaks, ``cancel`` tears down the sibling pump so the whole
    bridge exits together.
    """
    try:
        async for item in src:
            if isinstance(item, Exception):
                continue
            await dst.send(item)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
        pass
    finally:
        cancel()


async def _run_bridge(stdio_read, stdio_write, http_read, http_write) -> None:
    """Relay frames between the stdio client and the daemon's HTTP endpoint.

    Two pumps run concurrently — client→daemon and daemon→client — and the
    first to finish cancels the other. Takes the four already-open streams so
    the relay logic is drivable with in-memory streams under test.
    """
    async with anyio.create_task_group() as tg:
        cancel = tg.cancel_scope.cancel
        tg.start_soon(_pump, stdio_read, http_write, cancel)  # client → daemon
        tg.start_soon(_pump, http_read, stdio_write, cancel)  # daemon → client


async def _serve() -> None:
    """Open the real stdio and HTTP transports and relay between them."""
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    async with streamable_http_client(_mcp_endpoint()) as (http_read, http_write, _):
        async with stdio_server() as (stdio_read, stdio_write):
            await _run_bridge(stdio_read, stdio_write, http_read, http_write)


def run_stdio_bridge() -> None:
    """Entry point for ``schwab mcp --stdio``.

    Probes the daemon up front for a clear error when it's down, then relays
    until the client disconnects (EOF), Ctrl+C, or the connection drops.
    """
    url = _mcp_endpoint()
    if not probe_daemon(url):
        print(
            f"schwab mcp --stdio: daemon not reachable at {url}. "
            "Start it with `schwab server` (or set SCHWAB_DAEMON_URL).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        anyio.run(_serve)
    except KeyboardInterrupt:
        pass
    except (ConnectionError, OSError) as e:
        print(f"schwab mcp --stdio: bridge connection lost: {e}", file=sys.stderr)
        raise SystemExit(1) from e
