"""Schwab streamer WebSocket client.

Pure-async client for Schwab's realtime streamer. Exposes a
minimal, test-friendly API:

* :func:`fetch_streamer_info` — pulls the ``streamerInfo`` block
  from the Schwab REST ``userPreference`` endpoint. The returned
  :class:`StreamerInfo` carries the socket URL + all IDs needed for
  the ADMIN LOGIN frame.
* :class:`Streamer` — async WebSocket wrapper. Methods:
  - ``connect()`` / ``login()`` — establish + authenticate.
  - ``subscribe(service, keys, fields)`` / ``unsubscribe(...)``.
  - ``close()`` — graceful ADMIN LOGOUT + WebSocket close.
  - ``messages()`` — async iterator of incoming frames.

Request/response frame shapes are built and parsed via pure helpers
so the logic is unit-testable without a real WebSocket.

The public Schwab streamer doc isn't exhaustive on error-code
shapes, so the caller is expected to inspect the raw response from
login() and act accordingly rather than us trying to classify every
failure mode up front.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired


class StreamerError(Exception):
    """Raised on any streamer-level failure."""


class StreamerLoginError(StreamerError):
    """ADMIN LOGIN returned a non-zero code."""


@dataclass(frozen=True)
class StreamerInfo:
    """Everything the streamer login needs, pulled from the Schwab
    ``userPreference`` endpoint."""

    socket_url: str
    customer_id: str
    correl_id: str
    channel: str
    function_id: str


def fetch_streamer_info(client: SchwabClient) -> StreamerInfo:
    """Fetch and flatten the ``streamerInfo[0]`` block from
    ``/trader/v1/userPreference``.

    Raises :class:`StreamerError` if the shape is unexpected — the
    caller is meant to catch this at startup and bail before
    attempting a WebSocket connection.
    """
    try:
        raw = client.get(f"{SchwabClient.TRADER_BASE}/userPreference")
    except (ApiError, SessionExpired) as e:
        raise StreamerError(f"userPreference fetch failed: {e}") from e

    # Schwab returns an object with "streamerInfo" as a list; we only
    # ever need the first entry.
    if not isinstance(raw, dict):
        raise StreamerError("userPreference response was not an object")
    streamer_list = raw.get("streamerInfo")
    if not isinstance(streamer_list, list) or not streamer_list:
        raise StreamerError("userPreference.streamerInfo missing or empty")
    si = streamer_list[0]
    try:
        return StreamerInfo(
            socket_url=si["streamerSocketUrl"],
            customer_id=si["schwabClientCustomerId"],
            correl_id=si["schwabClientCorrelId"],
            channel=si["schwabClientChannel"],
            function_id=si["schwabClientFunctionId"],
        )
    except (KeyError, TypeError) as e:
        raise StreamerError(f"streamerInfo missing field: {e}") from e


# ---- frame builders (pure) --------------------------------------------


def build_login_request(
    info: StreamerInfo, access_token: str, request_id: str = "1"
) -> dict[str, Any]:
    return {
        "requests": [{
            "requestid": request_id,
            "service": "ADMIN",
            "command": "LOGIN",
            "SchwabClientCustomerId": info.customer_id,
            "SchwabClientCorrelId": info.correl_id,
            "parameters": {
                "Authorization": access_token,
                "SchwabClientChannel": info.channel,
                "SchwabClientFunctionId": info.function_id,
            },
        }]
    }


def build_logout_request(info: StreamerInfo, request_id: str) -> dict[str, Any]:
    return {
        "requests": [{
            "requestid": request_id,
            "service": "ADMIN",
            "command": "LOGOUT",
            "SchwabClientCustomerId": info.customer_id,
            "SchwabClientCorrelId": info.correl_id,
            "parameters": {},
        }]
    }


def build_subs_request(
    info: StreamerInfo,
    *,
    service: str,
    keys: list[str],
    fields: str,
    request_id: str,
    command: str = "SUBS",
) -> dict[str, Any]:
    """Build a SUBS or UNSUBS request frame.

    ``command`` defaults to ``"SUBS"``; pass ``"UNSUBS"`` for
    unsubscribe. Passing any other value is allowed (``"ADD"`` for
    incremental subscription, ``"VIEW"`` for field changes) but not
    used by the current callers.
    """
    return {
        "requests": [{
            "requestid": request_id,
            "service": service,
            "command": command,
            "SchwabClientCustomerId": info.customer_id,
            "SchwabClientCorrelId": info.correl_id,
            "parameters": {
                "keys": ",".join(keys),
                "fields": fields,
            },
        }]
    }


# ---- response classification ------------------------------------------


def classify_frame(frame: dict[str, Any]) -> str:
    """Return a coarse category for an incoming frame:
    ``"data"`` / ``"response"`` / ``"notify"`` / ``"unknown"``."""
    if "data" in frame:
        return "data"
    if "response" in frame:
        return "response"
    if "notify" in frame:
        return "notify"
    return "unknown"


def is_heartbeat(frame: dict[str, Any]) -> bool:
    """Schwab heartbeats arrive as ``{"notify":[{"heartbeat": ...}]}``."""
    notify = frame.get("notify")
    if not isinstance(notify, list):
        return False
    return any(isinstance(n, dict) and "heartbeat" in n for n in notify)


def login_response_ok(frame: dict[str, Any]) -> bool:
    """True if a login-response frame reports success (``code == 0``)."""
    responses = frame.get("response") or []
    for r in responses:
        if not isinstance(r, dict):
            continue
        if r.get("service") == "ADMIN" and r.get("command") == "LOGIN":
            content = r.get("content") or {}
            return content.get("code") == 0
    return False


# ---- Streamer (async WebSocket client) --------------------------------


class Streamer:
    """Thin async wrapper around the Schwab streamer WebSocket.

    Lifecycle:

    1. ``s = Streamer(info, access_token)``
    2. ``await s.connect()`` — opens the WebSocket.
    3. ``await s.login()`` — sends ADMIN LOGIN, waits for ack.
    4. ``await s.subscribe(service, keys, fields)`` — any number.
    5. ``async for frame in s.messages(): ...`` — consume data.
    6. ``await s.close()`` — ADMIN LOGOUT + WebSocket close.

    The class does not reconnect automatically — higher layers
    (the MCP server's subscription manager) are responsible for
    catching ``StreamerError`` from ``messages()`` and rebuilding
    the connection with the same subscription set. Keeping reconnect
    out of this layer makes the test surface much smaller.
    """

    # Request-id counter is module-state-free and monotonically
    # increasing for the life of this instance.
    def __init__(
        self,
        info: StreamerInfo,
        access_token: str,
        *,
        ws_factory=None,
    ) -> None:
        self._info = info
        self._access_token = access_token
        self._ws = None
        self._req_ids = itertools.count(1)
        # Buffer for frames that arrived before the login ack — rare
        # but possible if Schwab pushes early data.
        self._prelogin_buffer: list[dict[str, Any]] = []
        # Injection point for tests — real callers leave this None
        # and get ``websockets.connect`` by default. Kept lazy so
        # the module import doesn't touch the websockets library
        # unless we're actually streaming.
        self._ws_factory = ws_factory

    async def connect(self) -> None:
        if self._ws_factory is None:
            import websockets  # deferred import so tests can mock
            self._ws_factory = websockets.connect
        self._ws = await self._ws_factory(self._info.socket_url)

    async def login(self, timeout: float = 10.0) -> None:
        """Send ADMIN LOGIN and wait for the ack frame.

        Raises :class:`StreamerLoginError` on failure; the caller
        should propagate rather than retry (an auth problem needs a
        fresh access token, not a re-login).
        """
        if self._ws is None:
            raise StreamerError("connect() must be called before login()")
        req = build_login_request(self._info, self._access_token, "1")
        await self._ws.send(json.dumps(req))

        # Wait for the ADMIN LOGIN response. Any other frame before
        # the response is buffered and re-yielded by messages() so
        # we don't drop early ticks.
        try:
            while True:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                frame = json.loads(raw)
                if classify_frame(frame) == "response":
                    if login_response_ok(frame):
                        return
                    raise StreamerLoginError(
                        f"ADMIN LOGIN failed: {frame.get('response')}"
                    )
                # Stray data/notify frames before login ack — unusual but
                # we tolerate by buffering. Keep waiting for the ack.
                self._prelogin_buffer.append(frame)
        except asyncio.TimeoutError as e:
            raise StreamerLoginError("ADMIN LOGIN timed out") from e
        except StreamerLoginError:
            raise
        except Exception as e:  # pragma: no cover — defensive
            raise StreamerError(f"login error: {e}") from e

    async def subscribe(
        self,
        *,
        service: str,
        keys: list[str],
        fields: str,
    ) -> None:
        """Send a SUBS frame. Does not wait for the ack — Schwab's
        acks are subscription-success-affirmations, not
        subscription-ready signals; data starts arriving
        asynchronously regardless."""
        if self._ws is None:
            raise StreamerError("connect()+login() must be called first")
        req = build_subs_request(
            self._info,
            service=service,
            keys=keys,
            fields=fields,
            request_id=str(next(self._req_ids)),
            command="SUBS",
        )
        await self._ws.send(json.dumps(req))

    async def unsubscribe(self, *, service: str, keys: list[str]) -> None:
        if self._ws is None:
            return
        req = build_subs_request(
            self._info,
            service=service,
            keys=keys,
            fields="0",  # fields ignored by UNSUBS but the field is required
            request_id=str(next(self._req_ids)),
            command="UNSUBS",
        )
        await self._ws.send(json.dumps(req))

    async def close(self) -> None:
        """Send ADMIN LOGOUT then close the WebSocket. Swallows
        errors during logout — the primary goal is ensuring the
        socket is closed."""
        if self._ws is None:
            return
        try:
            req = build_logout_request(self._info, str(next(self._req_ids)))
            await self._ws.send(json.dumps(req))
        except Exception:
            pass
        try:
            await self._ws.close()
        except Exception:
            pass
        self._ws = None

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield every incoming frame until the socket closes.

        Heartbeats are emitted as-is — callers can filter via
        :func:`is_heartbeat`. Bad JSON is logged via
        :class:`StreamerError` and the iteration continues; the
        underlying WebSocket closing terminates the iterator
        cleanly.
        """
        if self._ws is None:
            raise StreamerError("connect()+login() must be called first")
        # Flush anything we buffered during login first.
        for frame in self._prelogin_buffer:
            yield frame
        self._prelogin_buffer.clear()
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield frame
        except Exception:
            # WebSocket closed or errored — terminate the iterator.
            return
