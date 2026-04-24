"""`stream` command — watch live Schwab quotes in the terminal.

Two transports:

* **MCP** (``--mcp`` or auto-selected when a daemon is reachable):
  connects to a running ``schwab_cli mcp --sse`` daemon as an MCP
  client, calls the ``stream_quote`` tool, and prints every progress
  notification payload. All subscribe / unsubscribe / streamer
  events show up in the daemon's log.
* **Direct** (``--direct`` or auto-fallback): opens our own Schwab
  streamer WebSocket. Useful when no daemon is running or for
  debugging; note this counts against Schwab's one-session-per-
  account limit and will clobber a running daemon's connection.

Auto-selection prefers MCP when the daemon is reachable. The probe
is a cheap TCP connect to the SSE port (default 127.0.0.1:7234).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import SchwabClient
from schwab_cli.api.streamer import (
    Streamer,
    classify_frame,
    fetch_streamer_info,
    is_heartbeat,
)
from schwab_cli.api.streamer_fields import decode, default_fields
from schwab_cli.session import load as load_session


_SERVICE = "LEVELONE_EQUITIES"


def run(
    symbols: list[str],
    *,
    fields: str | None,
    as_json: bool,
    via_mcp: bool,
    direct: bool,
    mcp_url: str,
) -> None:
    """Entry point from :mod:`schwab_cli.cli`."""
    if via_mcp and direct:
        typer.secho(
            "--mcp and --direct are mutually exclusive.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)
    if not symbols:
        typer.secho("stream requires at least one symbol.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Auto-probe: try MCP first if the daemon is reachable, fall
    # back to direct otherwise. Forced by --mcp / --direct.
    use_mcp = via_mcp
    if not direct and not via_mcp:
        if _probe_mcp_daemon(mcp_url):
            use_mcp = True

    if use_mcp:
        try:
            asyncio.run(_run_via_mcp(symbols, mcp_url=mcp_url, as_json=as_json))
        except _McpUnreachable:
            if via_mcp:
                typer.secho(
                    f"Could not reach MCP daemon at {mcp_url}.",
                    fg=typer.colors.RED, err=True,
                )
                raise typer.Exit(code=1)
            typer.secho(
                "(MCP daemon probe succeeded but connection failed; "
                "falling back to direct streamer)",
                fg=typer.colors.YELLOW, err=True,
            )
            asyncio.run(_run_direct(symbols, fields=fields, as_json=as_json))
        return

    # Direct path — open our own Schwab WebSocket.
    asyncio.run(_run_direct(symbols, fields=fields, as_json=as_json))


class _McpUnreachable(Exception):
    """Raised internally when the MCP daemon TCP-accepts but the
    JSON-RPC handshake fails — caller may fall back to direct mode."""


def _probe_mcp_daemon(url: str) -> bool:
    """Cheap TCP probe for the SSE port. Doesn't do a full HTTP
    request — just confirms something is listening, which is enough
    to decide whether to attempt the full MCP client handshake."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7234
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


async def _run_via_mcp(
    symbols: list[str],
    *,
    mcp_url: str,
    as_json: bool,
) -> None:
    """Connect to a running MCP daemon and stream stream_quote
    progress notifications to stdout. Ctrl+C cancels cleanly.

    Progress notifications are delivered via ``ClientSession``'s
    ``progress_callback`` parameter — passing a callback causes the
    SDK to auto-generate a ``progressToken`` for the request and
    route matching notifications to the callback.
    """
    # Deferred imports — avoid pulling in the MCP client unless we
    # actually need it (direct-mode users shouldn't pay that cost).
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    sse_url = mcp_url.rstrip("/")
    if not sse_url.endswith("/sse"):
        sse_url = sse_url + "/sse"

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        """Receive and render one progress notification."""
        if message is None or message == "keepalive":
            return
        try:
            decoded = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            typer.echo(str(message))
            return
        _render(decoded, as_json=as_json)

    try:
        async with sse_client(sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # Fire the streaming tool with a progress callback —
                # the SDK generates a progressToken automatically and
                # routes progress notifications to `on_progress`. The
                # call returns only on cancellation or error.
                await session.call_tool(
                    "stream_quote",
                    arguments={"symbols": [s.upper() for s in symbols]},
                    progress_callback=on_progress,
                )
    except (ConnectionError, OSError) as e:
        raise _McpUnreachable(str(e))
    except KeyboardInterrupt:
        return


async def _run_direct(
    symbols: list[str],
    *,
    fields: str | None,
    as_json: bool,
) -> None:
    cfg = config_module.load()
    if cfg is None:
        typer.secho("No config. Run `schwab_cli setup`.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho("No session. Run `schwab_cli auth`.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if session.refresh_token_expires_at <= int(time.time()):
        typer.secho("Refresh token expired. Run `schwab_cli auth --force`.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = SchwabClient(cfg, session)
    try:
        info = fetch_streamer_info(client)
    except Exception as e:
        typer.secho(f"streamer info fetch failed: {e}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    field_str = _resolve_fields(fields)

    streamer = Streamer(info, client.session.access_token)
    await streamer.connect()
    try:
        await streamer.login()
        await streamer.subscribe(
            service=_SERVICE,
            keys=[s.upper() for s in symbols],
            fields=field_str,
        )
        async for frame in streamer.messages():
            if is_heartbeat(frame):
                continue
            if classify_frame(frame) != "data":
                continue
            for chunk in frame.get("data", []):
                service = chunk.get("service") or _SERVICE
                for content in chunk.get("content", []):
                    decoded = decode(service, content)
                    _render(decoded, as_json=as_json)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await streamer.unsubscribe(
                service=_SERVICE,
                keys=[s.upper() for s in symbols],
            )
        finally:
            await streamer.close()


def _resolve_fields(requested: str | None) -> str:
    """Translate a friendly ``--fields bid,ask,last`` list into the
    numeric field-ID string Schwab expects. Default returns the full
    sensible set from :mod:`schwab_cli.api.streamer_fields`.
    """
    if not requested:
        return default_fields(_SERVICE)
    # Minimal friendly → numeric map, enough for common CLI use.
    friendly = {
        "symbol": "0", "bid": "1", "ask": "2", "last": "3",
        "bid_size": "4", "ask_size": "5",
        "volume": "8", "last_size": "9",
        "high": "10", "low": "11", "close": "12", "open": "17",
        "net_change": "18", "net_change_pct": "42",
        "mark": "33", "quote_time": "34", "trade_time": "35",
    }
    ids: list[str] = ["0"]  # always include symbol
    for raw in requested.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        numeric = friendly.get(key, key)  # pass-through for raw numeric IDs
        if numeric not in ids:
            ids.append(numeric)
    return ",".join(ids)


def _render(decoded: dict[str, Any], *, as_json: bool) -> None:
    """Print one update to stdout."""
    if as_json:
        typer.echo(json.dumps(decoded, default=str))
        return
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    sym = decoded.get("symbol", "?")
    parts = [f"[{ts}] {sym:6}"]
    # Preferred order if present; other fields appended after.
    preferred_keys = [
        "bid", "ask", "last", "bid_size", "ask_size", "volume",
        "net_change", "net_change_pct", "mark",
    ]
    seen: set[str] = {"symbol"}
    for key in preferred_keys:
        if key in decoded:
            seen.add(key)
            parts.append(f"{key} {_fmt_value(decoded[key])}")
    for key, val in decoded.items():
        if key in seen:
            continue
        parts.append(f"{key} {_fmt_value(val)}")
    typer.echo("  ".join(parts))


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)
