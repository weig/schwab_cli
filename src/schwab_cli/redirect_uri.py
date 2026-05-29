"""Parse and classify OAuth redirect URIs for the local callback server.

This module is intentionally tiny and dependency-free: it only knows how
to break a redirect URI into its ``(scheme, host, port, path)`` parts and
to decide whether a URI is a *loopback HTTPS* URI (the only shape the
local callback server is allowed to bind).
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

_DEFAULT_PORTS = {"https": 443, "http": 80}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class CallbackTarget:
    """The parsed components of a redirect URI."""

    scheme: str
    host: str
    port: int
    path: str


def parse_callback_uri(uri: str) -> CallbackTarget:
    """Parse ``uri`` into a :class:`CallbackTarget`.

    Rules:
      * ``https`` defaults to port 443, ``http`` to port 80; an explicit
        port in the URI always wins.
      * An empty path component becomes ``"/"``.
      * IPv6 hosts have their surrounding brackets stripped, so
        ``[::1]`` becomes ``"::1"``.
    """
    parsed = urllib.parse.urlparse(uri)
    scheme = parsed.scheme
    host = parsed.hostname or ""
    port = parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    path = parsed.path or "/"
    return CallbackTarget(scheme=scheme, host=host, port=port, path=path)


def is_loopback_https(uri: str) -> bool:
    """Return ``True`` iff ``uri`` is an HTTPS URI pointing at a loopback host."""
    target = parse_callback_uri(uri)
    return target.scheme == "https" and target.host in _LOOPBACK_HOSTS
