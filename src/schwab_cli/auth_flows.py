"""Orchestrate the OAuth ``code`` capture as a race of handlers.

``get_auth_response(cfg)`` builds an OAuth ``state`` token, opens the
authorize URL in the user's default browser (also prints the URL to
stderr as a fallback), and races a set of handlers in daemon threads.
The first handler to produce a valid :data:`AuthResult` wins; the rest
are signalled to stop via a shared :class:`threading.Event`.

Today's handler set:

* :class:`UserInputHandler` — always present. Prompts the user to paste
  the code, querystring, or full redirect URL.
* :class:`CodeRelayHandler` — added when ``cfg.auth_flow == "code_relay"``
  and ``cfg.code_relay_url`` is set.

The :class:`AuthHandler` Protocol + :data:`AuthResult` union are the
extension seam for ``AuthServerHandler``-style handlers that return
fully-exchanged token bundles.
"""
from __future__ import annotations

import queue
import secrets
import threading
import webbrowser

import typer

from schwab_cli.auth_handlers import (
    AuthHandler,
    AuthResult,
    CodeRelayHandler,
    UserInputHandler,
)
from schwab_cli.config import Config
from schwab_cli.oauth import build_auth_url


class AuthFlowError(Exception):
    """Raised when the auth flow as a whole fails (e.g., bad config,
    every handler errored)."""


def get_auth_response(cfg: Config) -> AuthResult:
    """Run the full auth-code capture: open browser, race handlers, return result.

    Generates a fresh OAuth ``state`` token; each handler is responsible
    for verifying it against its response.
    """
    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(cfg, state=state)
    _open_and_print(auth_url)
    handlers = _build_handlers(cfg)
    return _race_handlers(handlers, expected_state=state)


def _open_and_print(auth_url: str) -> None:
    """Print the auth URL to stderr and best-effort open the default browser."""
    typer.echo(f"\nOpening browser to:\n  {auth_url}\n", err=True)
    typer.echo(
        "If the browser does not open automatically, copy the URL above "
        "and paste it into any browser.\n",
        err=True,
    )
    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        # URL is already printed; the user can still complete the flow
        # manually in any browser.
        pass


def _build_handlers(cfg: Config) -> list[AuthHandler]:
    """Pick which handlers join the race for this config.

    ``UserInputHandler`` is always present. ``CodeRelayHandler`` joins
    when ``auth_flow="code_relay"`` and a ``code_relay_url`` is set.
    """
    handlers: list[AuthHandler] = [UserInputHandler()]
    if cfg.auth_flow == "code_relay":
        if not cfg.code_relay_url:
            raise AuthFlowError(
                "auth_flow='code_relay' requires 'code_relay_url' in config"
            )
        handlers.append(CodeRelayHandler(cfg.code_relay_url))
    return handlers


def _race_handlers(
    handlers: list[AuthHandler], *, expected_state: str,
) -> AuthResult:
    """Run handlers concurrently; return the first ``AuthResult``.

    Each handler runs on a daemon thread. The first successful result
    wins; the shared ``cancel`` event tells the losers to stop. If every
    handler fails, raises :class:`AuthFlowError` aggregating their errors.
    """
    result_q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def _runner(h: AuthHandler) -> None:
        try:
            r = h.wait_for_response(expected_state=expected_state, cancel=cancel)
            result_q.put(("ok", h, r))
        except BaseException as e:  # noqa: BLE001 — propagate via queue
            result_q.put(("err", h, e))

    threads = [
        threading.Thread(target=_runner, args=(h,), daemon=True, name=type(h).__name__)
        for h in handlers
    ]
    for t in threads:
        t.start()

    errors: list[tuple[AuthHandler, BaseException]] = []
    while True:
        kind, handler, val = result_q.get()
        if kind == "ok":
            cancel.set()
            return val
        errors.append((handler, val))
        if len(errors) >= len(handlers):
            cancel.set()
            detail = "; ".join(
                f"{type(h).__name__}: {e}" for h, e in errors
            )
            raise AuthFlowError(f"all auth handlers failed: {detail}")
