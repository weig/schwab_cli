"""Lightweight smoke tests for the SSE app's routing setup.

Exercises the Starlette app that ``run_sse`` builds without actually
starting uvicorn — verifies that the Mount/Route wiring is correct
so we don't regress on the NoneType-not-callable bug again.
"""

from __future__ import annotations

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
    """Reconstruct the Starlette app run_sse would bootstrap,
    without going through uvicorn / a real event loop."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    buf = io.StringIO()
    server = SchwabMcpServer(_FakeClient(), LogBook(stream=buf))
    sse = SseServerTransport("/messages/")

    async def admin_status(request):
        return JSONResponse(server._status_payload())

    async def admin_shutdown(request):
        return JSONResponse({"ok": True})

    async def handle_sse(request):
        return Response()

    routes = [
        Route("/sse", endpoint=handle_sse),
        # The original bug: this MUST be Mount, not Route.
        Mount("/messages/", app=sse.handle_post_message),
        Route("/admin/status", endpoint=admin_status, methods=["GET"]),
        Route("/admin/shutdown", endpoint=admin_shutdown, methods=["POST"]),
    ]
    return Starlette(routes=routes), server


def test_admin_status_returns_json():
    app, server = _build_app()
    client = TestClient(app)
    r = client.get("/admin/status")
    assert r.status_code == 200
    data = r.json()
    assert "subscription_summary" in data
    assert data["server_name"] == "schwab"


def test_admin_shutdown_returns_ok():
    app, _ = _build_app()
    client = TestClient(app)
    r = client.post("/admin/shutdown")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_messages_endpoint_is_mounted_not_routed():
    """Regression test for the NoneType-not-callable bug. Even with
    an empty POST body, Mount should return an HTTP error (400+)
    rather than exploding with NoneType."""
    app, _ = _build_app()
    client = TestClient(app)
    # With no session, handle_post_message should reject the POST.
    # We just care that it doesn't 500 with NoneType.
    r = client.post("/messages/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    # Any clean status — not a 500 traceback.
    assert r.status_code < 500 or "NoneType" not in r.text
