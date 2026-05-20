"""Orchestrate the OAuth ``code`` capture as a race of handlers.

``get_auth_response(cfg)`` builds an OAuth ``state`` token, opens the
authorize URL in the user's default browser (also prints the URL to
stderr as a fallback), and races a set of handlers in daemon threads.
The first handler to produce a valid :data:`AuthResult` wins; the rest
are signalled to stop via a shared :class:`threading.Event`.

Handler race composition:

* :class:`UserInputHandler` — always present. Paste fallback.
* :class:`AutoLoginHandler` — added when ``cfg.auth_flow == "client"``
  and ``cfg.auto_login_command`` is set (and ``--manual`` is not passed).
  Stands up a local HTTP listener and spawns webauto-cli in remote mode.
* :class:`CodeRelayHandler` — added when ``cfg.auth_flow == "code_relay"``
  and ``cfg.code_relay_url`` is set.

Side-effect (not in race) for ``auth_flow == "code_relay"`` with
``auto_login_command`` set: an :class:`AutoLoginSupervisor` spawns
webauto-cli in ``--no-notify`` mode so the browser drives Schwab; the
relay captures the code; ``CodeRelayHandler`` polls and wins. The
supervisor is always torn down on race exit.

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
    AutoLoginHandler,
    AutoLoginSupervisor,
    CodeRelayHandler,
    UserInputHandler,
)
from schwab_cli.config import Config
from schwab_cli.oauth import build_auth_url
from schwab_cli.paths import config_dir


class AuthFlowError(Exception):
    """Raised when the auth flow as a whole fails (e.g., bad config,
    every handler errored)."""


def get_auth_response(cfg: Config, *, manual: bool = False) -> AuthResult:
    """Run the full auth-code capture: open browser, race handlers, return result.

    Generates a fresh OAuth ``state`` token; each handler is responsible
    for verifying it against its response.

    When ``cfg.auto_login_command`` is set and ``--manual`` is not passed,
    schwab_cli additionally manages a webauto-cli subprocess:

    * ``auth_flow="client"`` → :class:`AutoLoginHandler` is the race
      participant; the local HTTP listener is what webauto-cli POSTs to.
    * ``auth_flow="code_relay"`` → :class:`AutoLoginSupervisor` spawns
      webauto-cli in ``--no-notify`` mode as a side-effect; the race is
      won by :class:`CodeRelayHandler` polling the remote relay.

    Either way, the subprocess is terminated on every exit path.
    """
    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(cfg, state=state)
    # When auto-login will spawn webauto, webauto opens its own browser;
    # don't double up. The URL is still printed so the paste fallback in
    # ``UserInputHandler`` is usable if webauto stalls.
    auto_login_active = cfg.auto_login_command is not None and not manual
    _open_and_print(auth_url, open_browser=not auto_login_active)

    supervisor = _maybe_start_supervisor(
        cfg, manual=manual, auth_url=auth_url, state=state,
    )
    try:
        handlers = _build_handlers(
            cfg, manual=manual, auth_url=auth_url,
        )
        return _race_handlers(handlers, expected_state=state)
    finally:
        if supervisor is not None:
            supervisor.terminate()


def _open_and_print(auth_url: str, *, open_browser: bool = True) -> None:
    """Print the auth URL to stderr; optionally open the default browser.

    The URL is always printed so the paste fallback in
    :class:`UserInputHandler` is usable. When ``open_browser=False`` (set
    by ``get_auth_response`` when auto-login is active), the redundant
    default-browser open is skipped — webauto-cli opens its own.
    """
    if open_browser:
        typer.echo(f"\nOpening browser to:\n  {auth_url}\n", err=True)
        typer.echo(
            "If the browser does not open automatically, copy the URL above "
            "and paste it into any browser.\n",
            err=True,
        )
        try:
            webbrowser.open(auth_url)
        except webbrowser.Error:
            pass
    else:
        typer.secho(
            "\nAuto-login to schwab...",
            err=True, fg=typer.colors.CYAN,
        )


def _build_handlers(
    cfg: Config, *, manual: bool, auth_url: str,
) -> list[AuthHandler]:
    """Pick which handlers join the race for this config.

    When auto-login is active (``auto_login_command`` set AND not
    ``--manual``), the user cannot interact with the terminal — auth is
    running unattended (typical ``--force`` from a cron job or similar).
    ``UserInputHandler`` is **excluded** in that case. The race is just
    the auto-driven path:

      * ``auth_flow="client"`` → :class:`AutoLoginHandler` solo
        (its listener receives webauto's POST).
      * ``auth_flow="code_relay"`` → :class:`CodeRelayHandler` solo
        (an :class:`AutoLoginSupervisor` spawns webauto outside the race
        — see :func:`_maybe_start_supervisor`).

    When auto-login is NOT active (no command, or ``--manual``), the
    race is the human-driven path:

      * :class:`UserInputHandler` (paste fallback) is **always** present.
      * :class:`CodeRelayHandler` joins when ``auth_flow="code_relay"``.
    """
    auto_login_active = cfg.auto_login_command is not None and not manual

    if auto_login_active:
        handlers: list[AuthHandler] = []
        if cfg.auth_flow == "client":
            handlers.append(AutoLoginHandler(
                cfg.auto_login_command,
                auth_url=auth_url,
                stderr_log_dir=config_dir() / "auth-debug",
                timeout_seconds=float(cfg.auto_login_timeout_seconds),
            ))
        elif cfg.auth_flow == "code_relay":
            if not cfg.code_relay_url:
                raise AuthFlowError(
                    "auth_flow='code_relay' requires 'code_relay_url' in config"
                )
            handlers.append(CodeRelayHandler(cfg.code_relay_url))
        return handlers

    # Human-driven path: paste fallback + optional relay.
    handlers = [UserInputHandler()]
    if cfg.auth_flow == "code_relay":
        if not cfg.code_relay_url:
            raise AuthFlowError(
                "auth_flow='code_relay' requires 'code_relay_url' in config"
            )
        handlers.append(CodeRelayHandler(cfg.code_relay_url))
    return handlers


def _maybe_start_supervisor(
    cfg: Config, *, manual: bool, auth_url: str, state: str,
) -> AutoLoginSupervisor | None:
    """Spawn the side-effect supervisor for ``auth_flow="code_relay"``
    with auto-login configured; otherwise return None.

    The supervisor is responsible for keeping webauto-cli alive while
    :class:`CodeRelayHandler` polls the relay. It never participates in
    the race itself.
    """
    if cfg.auto_login_command is None or manual:
        return None
    if cfg.auth_flow != "code_relay":
        return None
    sup = AutoLoginSupervisor(
        cfg.auto_login_command,
        auth_url=auth_url,
        state=state,
        stderr_log_dir=config_dir() / "auth-debug",
        timeout_seconds=float(cfg.auto_login_timeout_seconds),
    )
    sup.start()
    return sup


def _race_handlers(
    handlers: list[AuthHandler], *, expected_state: str,
) -> AuthResult:
    """Run handlers concurrently; return the first ``AuthResult``.

    Each handler runs on a daemon thread. The first successful result
    wins (including :class:`ErrorResult` — OAuth errors are authoritative
    answers, not failures); the shared ``cancel`` event tells losers to
    stop. If every handler fails operationally, raises
    :class:`AuthFlowError` aggregating their errors.
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
        threading.Thread(
            target=_runner, args=(h,),
            daemon=True, name=type(h).__name__,
        )
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
