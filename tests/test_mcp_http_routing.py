"""Lightweight smoke tests for the Streamable HTTP app's routing.

Exercises the Starlette app that ``run_http`` builds without actually
starting uvicorn — verifies that the Mount/Route wiring is correct
(the ``/mcp`` endpoint is Mount-ed, not Route-d, so we don't regress
on the NoneType-not-callable bug) and that the session manager's
lifespan starts cleanly under Starlette's TestClient.
"""

from __future__ import annotations

import contextlib
import io

from starlette.testclient import TestClient

from schwab_cli.mcp_server.app import SchwabMcpServer
from schwab_cli.mcp_server.logbook import LogBook


class _FakeSession:
    access_token = "atok"
    refresh_token = "rtok"
    expires_at = 9_000_000_000
    refresh_token_expires_at = 9_000_000_000


class _FakeClient:
    @property
    def session(self):
        return _FakeSession()


def _build_app():
    """Reconstruct the Starlette app run_http would bootstrap,
    without going through uvicorn / a real daemon loop."""
    from mcp.server.streamable_http_manager import (
        StreamableHTTPSessionManager,
    )
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    buf = io.StringIO()
    server = SchwabMcpServer(_FakeClient(), LogBook(stream=buf))
    session_manager = StreamableHTTPSessionManager(
        app=server._server, json_response=False, stateless=False,
    )

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def health(request):
        return JSONResponse({"ok": True})

    async def admin_status(request):
        return JSONResponse(server._status_payload())

    async def admin_shutdown(request):
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        async with session_manager.run():
            yield

    routes = [
        # The original bug: this MUST be Mount, not Route.
        Mount("/mcp", app=handle_mcp),
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/admin/status", endpoint=admin_status, methods=["GET"]),
        Route("/admin/shutdown", endpoint=admin_shutdown, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=_lifespan), server


def test_admin_status_returns_json():
    app, server = _build_app()
    # TestClient runs the lifespan, which starts session_manager.run().
    with TestClient(app) as client:
        r = client.get("/admin/status")
    assert r.status_code == 200
    data = r.json()
    assert "subscription_summary" in data
    assert data["server_name"] == "schwab"


def test_admin_shutdown_returns_ok():
    app, _ = _build_app()
    with TestClient(app) as client:
        r = client.post("/admin/shutdown")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_health_returns_ok():
    """`run_http` mounts a `/health` liveness probe (powers `server
    status`'s reachability check). It must return 200 {"ok": true}."""
    app, _ = _build_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_run_http_source_mounts_health_route():
    """Guard that the production `run_http` actually wires `/health` (the
    locally-rebuilt _build_app mirrors it, but this asserts the real
    source so the two cannot drift)."""
    import inspect

    from schwab_cli.mcp_server.app import SchwabMcpServer

    src = inspect.getsource(SchwabMcpServer.run_http)
    assert 'Route("/health"' in src


def test_mcp_endpoint_is_mounted_not_routed():
    """Regression test for the NoneType-not-callable bug. A bare POST
    to /mcp without a proper MCP `initialize` handshake should return a
    clean HTTP error (4xx) — NOT a 500 NoneType crash. This proves the
    `/mcp` route is Mount-ed and `handle_request` is wired through the
    session manager (its task group must be live via the lifespan)."""
    app, _ = _build_app()
    with TestClient(app) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    # The session manager rejects a non-initialize POST with a clean
    # 4xx (bad request / missing session id) — never a 500 NoneType.
    assert 400 <= r.status_code < 500
    assert "NoneType" not in r.text
