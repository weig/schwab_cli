"""``/api/v1/stream`` — WebSocket quote streaming for the resource server.

Scope model (per the webauth design): ``streaming`` is a TRANSPORT
modifier — it grants the right to receive data over a stream, but each
subscription must ALSO be covered by the data scope that owns it
(quotes → ``marketdata``). ``streaming`` alone authorizes nothing.

Protocol (JSON text frames):

* client → server: ``{"action": "subscribe", "symbols": ["SPY", ...]}``
* server → client: ``{"type": "subscribed", "symbols": [...]}`` ack,
  then one ``{"type": "quote", ...decoded update...}`` frame per tick;
  errors come back as ``{"type": "error", "error": "..."}`` without
  closing the connection (a bad subscribe shouldn't kill good ones).

The connection shares the daemon's single Schwab websocket through the
StreamerBridge refcount table — subscriptions are dropped (and the
upstream UNSUBS fires when refcounts hit zero) when the client
disconnects. Close codes mirror HTTP: 4403 missing ``streaming`` scope,
1013 streamer unavailable (standalone REST mode has no bridge).

The webauth middleware has already authenticated the connection
(tier 1: peer allowlist + JWT) before this endpoint runs; in legacy
mode (no providers, loopback only) there is no principal and all
subscriptions are allowed — matching the rest of /api/v1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Callable

from schwab_cli.webauth.scopes import scope_satisfied

_MAX_SYMBOLS_PER_SUB = 50
_SERVICE = "LEVELONE_EQUITIES"
# Data scope required per streamer service — extend as more stream
# types appear (positions/accounts streams would map to their scopes).
_SERVICE_SCOPE = {_SERVICE: "marketdata"}


def stream_routes(get_bridge: Callable[[], Any]):
    """Return the streaming route list.

    ``get_bridge`` resolves the daemon's StreamerBridge at call time
    (late-bound: the MCP server owns it). Returning ``None`` means
    streaming is unavailable (standalone REST mode).
    """
    from starlette.routing import WebSocketRoute

    async def endpoint(websocket) -> None:
        await _serve(websocket, get_bridge)

    return [WebSocketRoute("/api/v1/stream", endpoint)]


async def _serve(websocket, get_bridge: Callable[[], Any]) -> None:
    from starlette.websockets import WebSocketDisconnect

    principal = websocket.scope.get("state", {}).get("principal")
    if principal is not None and not scope_satisfied(
        principal.scopes, "streaming",
    ):
        # Authenticated but not stream-entitled: reject the handshake.
        await websocket.close(code=4403)
        return

    bridge = get_bridge()
    if bridge is None:
        await websocket.close(code=1013)  # try again later — no streamer
        return

    await websocket.accept()
    session_id = f"ws_{id(websocket)}"
    pumps: list[asyncio.Task] = []
    sub_counter = 0

    async def _pump(queue: asyncio.Queue) -> None:
        while True:
            update = await queue.get()
            await websocket.send_text(json.dumps(
                {"type": "quote", **update}, default=str,
            ))

    async def _error(message: str) -> None:
        await websocket.send_text(json.dumps(
            {"type": "error", "error": message},
        ))

    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except (ValueError, KeyError):
                await _error("frames must be JSON objects")
                continue

            action = msg.get("action") if isinstance(msg, dict) else None
            if action != "subscribe":
                await _error("unsupported action (expected 'subscribe')")
                continue
            symbols = msg.get("symbols")
            if (
                not isinstance(symbols, list)
                or not symbols
                or not all(isinstance(s, str) and s for s in symbols)
            ):
                await _error("symbols must be a non-empty list of strings")
                continue
            if len(symbols) > _MAX_SYMBOLS_PER_SUB:
                await _error(f"too many symbols (max {_MAX_SYMBOLS_PER_SUB})")
                continue

            required = _SERVICE_SCOPE[_SERVICE]
            if principal is not None and not scope_satisfied(
                principal.scopes, required,
            ):
                # streaming grants the transport; the DATA scope still
                # gates what may flow over it.
                await _error(f"missing required scope: {required}")
                continue

            upper = [s.upper() for s in symbols]
            sub_counter += 1
            queue = await bridge.add_subscription(
                session_id, f"sub{sub_counter}", _SERVICE, upper,
            )
            pumps.append(asyncio.create_task(_pump(queue)))
            await websocket.send_text(json.dumps(
                {"type": "subscribed", "symbols": upper},
            ))
    except WebSocketDisconnect:
        pass
    finally:
        for task in pumps:
            task.cancel()
        # Shielded: subscription refcounts must unwind (firing upstream
        # UNSUBS at zero) even while the connection teardown cancels us.
        with contextlib.suppress(Exception):
            await asyncio.shield(bridge.drop_session(session_id))
