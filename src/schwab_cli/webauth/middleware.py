"""Two-tier ASGI gate in front of the daemon's HTTP surface.

* **Tier 1 — ``/api/*`` (the public resource-server surface):** the
  direct peer address must be in ``web.allow`` (nginx-style allowlist —
  IPs or CIDR networks, implicit deny; loopback always implied) AND the
  request must carry a JWT that one of the configured providers signed
  (``/api/v1/health`` is exempt from the JWT so a reverse proxy can
  health-check). With NO providers configured, /api keeps the
  pre-webauth behavior: loopback peers pass unauthenticated, anything
  else gets a 503 pointing at the missing configuration.
* **Tier 2 — everything else** (``/admin``, ``/auth``, ``/mcp``,
  ``/health``): loopback peers only, the allowlist cannot open these.
  A fat-fingered wide bind must never expose the internal control
  plane; remote peers get an unadorned 404 so the endpoints aren't
  advertised.

Hardening details:

* Paths are normalized (``posixpath.normpath``) BEFORE tier
  classification — ``/api/../admin/...`` is tier 2, not tier 1, so a
  remote scanner can't even learn that internal paths exist.
* Only the DIRECT peer is authenticated — forwarded-for headers are
  deliberately ignored (we trust the proxy by address, not by header).
* Loopback detection uses :mod:`ipaddress` so IPv4-mapped loopback
  (``::ffff:127.0.0.1`` on dual-stack binds) is recognized.
* Token verification runs in a worker thread: a JWKS cache miss does
  blocking HTTP and must not stall the event loop.
* WebSocket denials send a close frame (code ``4000+status``) instead
  of an HTTP response.
"""

from __future__ import annotations

import asyncio
import ipaddress
import posixpath
from typing import Callable, Iterable

from schwab_cli.webauth.scopes import scope_satisfied
from schwab_cli.webauth.verify import (
    Principal,
    SubjectNotAllowed,
    WebAuthError,
)

_API_PREFIX = "/api/"
_HEALTH_PATH = "/api/v1/health"
_MCP_PREFIX = "/mcp"
_PRM_PATH = "/.well-known/oauth-protected-resource"


def _is_loopback(peer: str | None) -> bool:
    if not peer:
        return False
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _normalized_path(scope) -> str:
    path = posixpath.normpath(scope.get("path", "") or "/")
    return "/" if path == "." else path


def _default_peer_of(scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


class WebAuthMiddleware:
    """Pure ASGI wrapper implementing the two-tier gate."""

    def __init__(
        self,
        app,
        *,
        verifier,
        has_providers: bool,
        allow: Iterable[str] = ("127.0.0.1", "::1"),
        peer_of: Callable[[dict], str | None] | None = None,
        mcp_resource_url: str | None = None,
        issuers: Iterable[str] = (),
        log: Callable[..., None] | None = None,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._has_providers = has_providers
        self._allow_nets = _parse_allow(allow)
        self._peer_of = peer_of or _default_peer_of
        # Master switch for the public /mcp surface (remote Claude
        # connector). Unset -> /mcp stays tier-2 loopback-only and the
        # PRM path 404s: behavior identical to pre-connector builds.
        self._mcp_resource_url = (mcp_resource_url or "").rstrip("/") or None
        self._issuers = tuple(issuers)
        # Remote traffic is invisible otherwise: uvicorn runs at warning level
        # and every gate decision below is a silent 401/403/404. Without this
        # there is no way to tell "the client never reached us" apart from
        # "we rejected it" when debugging a connector handshake.
        self._log = log

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path = _normalized_path(scope)
        peer = self._peer_of(scope)

        if self._mcp_resource_url is not None and (
            path == _PRM_PATH or path.startswith(_PRM_PATH + "/")
        ):
            # RFC 9728 Protected Resource Metadata — the connector's
            # discovery document. Public by design (no token) but only to
            # allowlisted peers; carries no secrets (issuer + resource).
            if self._peer_allowed(peer):
                self._audit("webauth.prm_served", peer=peer, path=path)
                await self._serve_prm(scope, receive, send)
            else:
                self._audit("webauth.deny", peer=peer, path=path, status=404,
                            reason="peer not allowed (prm)")
                await self._deny(scope, receive, send, 404, {
                    "error": "not found",
                })
            return

        if (
            self._mcp_resource_url is not None
            and not _is_loopback(peer)
            and (path == _MCP_PREFIX or path.startswith(_MCP_PREFIX + "/"))
        ):
            # Public /mcp surface: peer allowlist, then the SAME verifier
            # the /api tier uses. Loopback callers never reach this branch
            # and keep their unauthenticated access.
            await self._gate_remote_mcp(scope, receive, send)
            return

        if not path.startswith(_API_PREFIX):
            # Tier 2: internal control plane — loopback only. 404 (not
            # 403) so a remote scanner learns nothing about the paths.
            if _is_loopback(peer):
                await self._app(scope, receive, send)
            else:
                self._audit("webauth.deny", peer=peer, path=path, status=404,
                            reason="internal path, non-loopback peer")
                await self._deny(scope, receive, send, 404, {
                    "error": "not found",
                })
            return

        # Tier 1: /api/* — peer allowlist first, cheapest check.
        if not self._peer_allowed(peer):
            await self._deny(scope, receive, send, 403, {
                "error": "client address not allowed",
            })
            return

        if not self._has_providers:
            # Legacy (pre-webauth) behavior: loopback callers keep
            # working unauthenticated; anything remote needs providers.
            if _is_loopback(peer):
                await self._app(scope, receive, send)
            else:
                await self._deny(scope, receive, send, 503, {
                    "error": "webauth providers not configured",
                })
            return

        if path == _HEALTH_PATH:
            # Proxy liveness probe: allowlisted peer, no JWT required.
            await self._app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if token is None:
            await self._deny(
                scope, receive, send, 401,
                {"error": "missing bearer token"},
                www_authenticate='Bearer realm="schwab-api"',
            )
            return
        try:
            # Worker thread: a JWKS cache miss does blocking HTTP; the
            # event loop must keep serving other requests meanwhile.
            principal = await asyncio.to_thread(self._verifier.verify, token)
        except SubjectNotAllowed as e:
            await self._deny(scope, receive, send, 403, {"error": str(e)})
            return
        except WebAuthError as e:
            await self._deny(
                scope, receive, send, 401,
                {"error": str(e)},
                www_authenticate='Bearer error="invalid_token"',
            )
            return

        scope.setdefault("state", {})["principal"] = principal
        await self._app(scope, receive, send)

    def _audit(self, event: str, **fields) -> None:
        if self._log is not None:
            try:
                self._log(event, **fields)
            except Exception:  # noqa: BLE001 — logging must never break a request
                pass

    async def _serve_prm(self, scope, receive, send) -> None:
        from starlette.responses import JSONResponse

        response = JSONResponse({
            "resource": self._mcp_resource_url,
            "authorization_servers": list(self._issuers),
            "bearer_methods_supported": ["header"],
        })
        await response(scope, receive, send)

    def _www_authenticate(self, error: str | None = None) -> str:
        parts = ['Bearer realm="schwab-mcp"']
        if error:
            parts.append(f'error="{error}"')
        parts.append(
            f'resource_metadata="{self._mcp_resource_url}{_PRM_PATH}"'
        )
        return ", ".join(parts)

    async def _gate_remote_mcp(self, scope, receive, send) -> None:
        peer = self._peer_of(scope)
        path = _normalized_path(scope)
        if not self._peer_allowed(peer):
            self._audit("webauth.deny", peer=peer, path=path, status=404,
                        reason="peer not allowed (mcp)")
            await self._deny(scope, receive, send, 404, {
                "error": "not found",
            })
            return
        if not self._has_providers:
            self._audit("webauth.deny", peer=peer, path=path, status=503,
                        reason="no providers configured")
            await self._deny(scope, receive, send, 503, {
                "error": "webauth providers not configured",
            })
            return
        token = _bearer_token(scope)
        if token is None:
            self._audit("webauth.deny", peer=peer, path=path, status=401,
                        reason="missing bearer token")
            await self._deny(
                scope, receive, send, 401,
                {"error": "missing bearer token"},
                www_authenticate=self._www_authenticate(),
            )
            return
        try:
            principal = await asyncio.to_thread(self._verifier.verify, token)
        except SubjectNotAllowed as e:
            # The single most useful debugging line: the token was valid but
            # its subject is not on the allowlist — shows the exact sub to add.
            self._audit("webauth.deny", peer=peer, path=path, status=403,
                        reason=f"subject not allowed: {e}")
            await self._deny(scope, receive, send, 403, {"error": str(e)})
            return
        except WebAuthError as e:
            self._audit("webauth.deny", peer=peer, path=path, status=401,
                        reason=f"invalid token: {e}")
            await self._deny(
                scope, receive, send, 401,
                {"error": str(e)},
                www_authenticate=self._www_authenticate("invalid_token"),
            )
            return
        self._audit("webauth.allow", peer=peer, path=path,
                    subject=principal.subject, provider=principal.provider)
        scope.setdefault("state", {})["principal"] = principal
        await self._app(scope, receive, send)

    def _peer_allowed(self, peer: str | None) -> bool:
        if _is_loopback(peer):
            return True
        if not peer:
            return False
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(ip in net for net in self._allow_nets)

    @staticmethod
    async def _deny(
        scope, receive, send, status: int, payload: dict,
        *, www_authenticate: str | None = None,
    ) -> None:
        if scope["type"] == "websocket":
            # No HTTP response on a websocket scope — close the
            # handshake with a 4xxx application code mirroring the
            # would-be HTTP status.
            await send({"type": "websocket.close", "code": 4000 + status})
            return
        from starlette.responses import JSONResponse

        headers = (
            {"WWW-Authenticate": www_authenticate} if www_authenticate else None
        )
        response = JSONResponse(payload, status_code=status, headers=headers)
        await response(scope, receive, send)


def _parse_allow(allow: Iterable[str]) -> tuple:
    """Parse allowlist entries into networks (IPs become /32 / /128).

    Config validation rejects bad entries up front; this defensive skip
    keeps the middleware total for direct constructions in tests.
    """
    nets = []
    for entry in allow:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _bearer_token(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1")
            prefix, _, token = text.partition(" ")
            if prefix.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


def scope_denial(request, required: str):
    """Route-level scope requirement; ``None`` means proceed.

    Returns a 403 ``JSONResponse`` when the authenticated principal
    lacks ``required``. In legacy mode (no providers → no principal,
    loopback-only access already enforced by the middleware) everything
    is allowed, matching the pre-webauth behavior.
    """
    principal: Principal | None = request.scope.get("state", {}).get("principal")
    if principal is None:
        return None
    if scope_satisfied(principal.scopes, required):
        return None
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": f"missing required scope: {required}"}, status_code=403,
    )
