"""Run a :class:`TokenManager` inside the ``schwab server`` daemon.

Phase-2 glue: builds the manager with its notifications bridged into the
Notifier infra, runs the two tracks as daemon threads, and tears them
down on shutdown. All three server modes (bare / --enable-mcp /
--enable-rest) share this wiring.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable

from schwab_cli.server.token_manager import TokenManager

if TYPE_CHECKING:
    from schwab_cli.config import Config
    from schwab_cli.notify import Notifier
    from schwab_cli.session import Session

DEFAULT_JOIN_TIMEOUT_S = 5.0


def _notifier_emit(notifier: "Notifier | None") -> Callable[..., None]:
    """Wrap ``notifier.emit`` so a broken notifier can never break a track."""
    if notifier is None:
        return lambda event, **fields: None

    def _emit(event: str, **fields) -> None:
        try:
            notifier.emit(event, **fields)
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

    return _emit


def build_token_manager(
    cfg: "Config",
    *,
    notifier: "Notifier | None" = None,
    on_session_replaced: Callable[["Session"], None] | None = None,
    **overrides: Any,
) -> TokenManager:
    """Build the daemon's TokenManager.

    ``notifier`` is the Notifier infra (or None); its ``emit`` is wrapped
    best-effort. ``overrides`` pass through to :class:`TokenManager` for
    tests (clock, exchange, session I/O, ...).
    """
    return TokenManager(
        cfg,
        emit=_notifier_emit(notifier),
        on_session_replaced=on_session_replaced,
        **overrides,
    )


def start_token_threads(
    mgr: TokenManager, stop: threading.Event,
) -> tuple[threading.Thread, threading.Thread]:
    """Start the access + refresh tracks as named daemon threads."""
    access = threading.Thread(
        target=mgr.access_loop, args=(stop,),
        daemon=True, name="schwab-token-access",
    )
    refresh = threading.Thread(
        target=mgr.refresh_loop, args=(stop,),
        daemon=True, name="schwab-token-refresh",
    )
    access.start()
    refresh.start()
    return access, refresh


def stop_token_threads(
    mgr: TokenManager,
    stop: threading.Event,
    threads: tuple[threading.Thread, ...],
    *,
    timeout: float = DEFAULT_JOIN_TIMEOUT_S,
) -> None:
    """Signal both tracks to stop and join them (best-effort)."""
    stop.set()
    mgr.wake()
    for t in threads:
        t.join(timeout=timeout)
