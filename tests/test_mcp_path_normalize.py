"""`/mcp` must be served without an HTTP redirect.

Starlette's Mount redirects `/mcp` → `/mcp/` with a 307 whose Location is an
ABSOLUTE URL built from the origin's own scheme/host. Behind a tunnel that is
`http://<internal-ip>/mcp/`: cross-scheme, unreachable, and clients routinely
drop the Authorization header when following it — which is exactly how a
remote MCP client fails immediately after authenticating successfully.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from schwab_cli.mcp_server.app import _normalize_mcp_path


async def _inner(scope, receive, send):
    await JSONResponse({"path": scope["path"]})(scope, receive, send)


def _client(**kw) -> TestClient:
    app = Starlette(routes=[
        Mount("/mcp", app=_inner),
        Route("/health", lambda r: JSONResponse({"ok": True})),
    ])
    return TestClient(_normalize_mcp_path(app), follow_redirects=False, **kw)


def test_bare_mcp_is_served_without_redirect():
    r = _client().post("/mcp")
    assert r.status_code == 200          # not 307
    assert "location" not in {k.lower() for k in r.headers}


def test_trailing_slash_still_works():
    assert _client().post("/mcp/").status_code == 200


def test_subpaths_are_untouched():
    """Only the bare `/mcp` is rewritten; deeper paths pass through as-is."""
    seen = {}

    async def capture(scope, receive, send):
        seen["path"] = scope["path"]
        await JSONResponse({})(scope, receive, send)

    TestClient(_normalize_mcp_path(capture)).post("/mcp/messages")
    assert seen["path"] == "/mcp/messages"


def test_other_routes_unaffected():
    assert _client().get("/health").json() == {"ok": True}


def test_unrelated_prefix_not_rewritten():
    """`/mcpx` must not be mangled into `/mcpx/`."""
    app = Starlette(routes=[Route("/mcpx", lambda r: JSONResponse({"hit": 1}))])
    c = TestClient(_normalize_mcp_path(app), follow_redirects=False)
    assert c.get("/mcpx").json() == {"hit": 1}


def test_normalizer_does_not_mutate_caller_scope():
    """The wrapper copies the scope — a shared dict must not leak the edit."""
    seen = {}

    async def capture(scope, receive, send):
        seen["path"] = scope["path"]
        await JSONResponse({})(scope, receive, send)

    scope_holder: dict = {}

    async def outer(scope, receive, send):
        scope_holder["original"] = scope
        await _normalize_mcp_path(capture)(scope, receive, send)

    TestClient(outer).post("/mcp")
    assert seen["path"] == "/mcp/"                       # inner app sees the fix
    assert scope_holder["original"]["path"] == "/mcp"    # caller's scope intact


def test_proxy_headers_stay_disabled():
    """Regression guard for the deploy that broke the tunnel.

    Honouring X-Forwarded-For rewrites scope["client"] to the ORIGINAL caller,
    which silently redefines `web.allow`: the allowlist names the reverse proxy
    permitted to front us and includes loopback, so keying it on a forwarded
    value both rejects the tunnel peer and would make the loopback entries
    reachable from off-box. Nothing builds request-derived absolute URLs, so
    there is no upside to trade against that.
    """
    import inspect

    from schwab_cli.mcp_server import app as app_mod

    src = inspect.getsource(app_mod.SchwabMcpServer.run_http)
    assert "proxy_headers=True" not in src
    assert "forwarded_allow_ips" not in src
