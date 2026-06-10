"""Compose the webauth gate for the server daemon (P2 wiring).

One call at startup loads the provider files, surfaces their errors
through the injected ``warn`` sink (stderr + logbook in the daemon),
and returns an ASGI wrapper that both REST-capable server modes apply
to their app. With zero providers the wrapper still enforces tier-2
loopback isolation and keeps /api in the legacy loopback-unauth mode.
"""

from __future__ import annotations

from typing import Callable, Iterable

from schwab_cli.webauth.config import LoadedProviders, load_providers
from schwab_cli.webauth.middleware import WebAuthMiddleware
from schwab_cli.webauth.verify import TokenVerifier


def build_gate(
    *,
    allow: Iterable[str],
    warn: Callable[[str], None] | None = None,
) -> tuple[Callable[[object], object], LoadedProviders]:
    """Load providers and return ``(wrap_asgi, loaded)``.

    ``wrap_asgi(app)`` applies the two-tier WebAuthMiddleware.
    Provider-file problems are reported through ``warn`` (one line per
    file) and never block startup.
    """
    loaded = load_providers()
    if warn is not None:
        for err in loaded.errors:
            warn(f"webauth: provider DISABLED — {err.path}: {err.reason}")
    verifier = TokenVerifier(loaded.providers) if loaded.providers else None
    allow_tuple = tuple(allow)

    def wrap(app):
        return WebAuthMiddleware(
            app,
            verifier=verifier,
            has_providers=bool(loaded.providers),
            allow=allow_tuple,
        )

    return wrap, loaded
