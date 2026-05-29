"""Orchestrate the OAuth ``code`` capture as a race of handlers.

``get_auth_response(cfg)`` builds an OAuth ``state`` token, opens the
authorize URL in the user's default browser (also prints the URL to
stderr as a fallback), and races a set of handlers in daemon threads.
The first handler to produce a valid :data:`AuthResult` wins; the rest
are signalled to stop via a shared :class:`threading.Event`.

Only the ``local_server`` flow is supported. Legacy ``auth_flow`` values
(``code_relay`` / ``client``) load fine (so non-auth commands keep
working) but raise an actionable :class:`AuthFlowError` here.

Handler race composition for ``local_server``:

* :class:`UserInputHandler` — paste fallback. Present on the human path
  (no ``auto_login_command``, or ``--manual``); excluded when auto-login
  is driving the flow unattended.
* :class:`LocalServerHandler` — binds a loopback HTTPS callback server on
  ``127.0.0.1`` and captures the IdP redirect directly.

Side-effect (not in race) when ``auto_login_command`` is set and
``--manual`` is not passed: an :class:`AutoLoginSupervisor` spawns
webauto-cli in ``--no-notify`` mode so the browser drives Schwab; the
local callback server captures the code; the handler wins. The
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
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from schwab_cli.session import Session

from schwab_cli.auth_callback_server import CallbackServerError, LocalServerHandler
from schwab_cli.auth_handlers import (
    AuthHandler,
    AuthResult,
    AutoLoginSupervisor,
    UserInputHandler,
)
from schwab_cli.cert.keychain import MacTrustStore
from schwab_cli.cert.manager import CertManager, LeafAbsentError
from schwab_cli.config import AUTH_FLOWS, Config
from schwab_cli.oauth import build_auth_url
from schwab_cli.paths import config_dir
from schwab_cli.redirect_uri import is_loopback_https


class AuthFlowError(Exception):
    """Raised when the auth flow as a whole fails (e.g., bad config,
    every handler errored)."""


def _resolve_cert_paths(redirect_uri: str) -> tuple[str, str] | tuple[None, None]:
    """Resolve the TLS cert/key paths the local callback server needs.

    * Loopback-HTTPS redirect URI → return ``(certfile, keyfile)`` for the
      installed leaf certificate. Raises :class:`AuthFlowError` (pointing
      at ``schwab cert install``) when the leaf is not on disk.
    * Anything else → ``(None, None)`` (no TLS material needed).
    """
    if not is_loopback_https(redirect_uri):
        return (None, None)
    try:
        leaf = CertManager(MacTrustStore()).leaf_paths()
    except LeafAbsentError as e:
        raise AuthFlowError(
            "local callback server needs a TLS certificate — run "
            "`schwab cert install` first."
        ) from e
    return (str(leaf.cert), str(leaf.key))


def get_auth_response(cfg: Config, *, manual: bool = False) -> AuthResult:
    """Run the full auth-code capture: open browser, race handlers, return result.

    Generates a fresh OAuth ``state`` token; each handler is responsible
    for verifying it against its response.

    Only the ``local_server`` flow is supported. A legacy ``auth_flow``
    (``code_relay`` / ``client``) loads fine but fails here with an
    actionable error directing the user to re-run ``schwab setup``.

    When ``cfg.auto_login_command`` is set and ``--manual`` is not passed,
    schwab_cli additionally manages a webauto-cli subprocess via an
    :class:`AutoLoginSupervisor` (``--no-notify`` mode): the browser
    drives Schwab and the :class:`LocalServerHandler` captures the
    redirect. The subprocess is terminated on every exit path.
    """
    if cfg.auth_flow not in AUTH_FLOWS:
        raise AuthFlowError(
            f"auth_flow {cfg.auth_flow!r} is no longer supported — "
            "re-run `schwab setup` to switch to the local callback server."
        )
    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(cfg, state=state)
    auto_login_active = cfg.auto_login_command is not None and not manual

    # Fail fast: resolve the TLS cert and BIND the callback port BEFORE we send
    # the user (or webauto) to the IdP. A missing cert (`cert install`) or an
    # in-use port surfaces as a clean AuthFlowError here — not after the user
    # has already logged in and burned the CSRF state token.
    handlers = _build_handlers(cfg, manual=manual, auth_url=auth_url)

    supervisor = None
    try:
        # When auto-login will spawn webauto, webauto opens its own browser;
        # don't double up. The URL is still printed so the paste fallback in
        # ``UserInputHandler`` is usable if webauto stalls.
        _open_and_print(auth_url, open_browser=not auto_login_active)
        supervisor = _maybe_start_supervisor(
            cfg, manual=manual, auth_url=auth_url, state=state,
        )
        return _race_handlers(handlers, expected_state=state)
    finally:
        if supervisor is not None:
            supervisor.terminate()
        # Release any bound callback port on every exit path (the winning
        # handler self-closes, but losers / early failures must be torn down).
        for h in handlers:
            close = getattr(h, "close", None)
            if callable(close):
                close()


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
    """Pick which handlers join the race for this ``local_server`` config.

    The :class:`LocalServerHandler` (loopback HTTPS callback server) is
    always a participant; its TLS material comes from the
    :func:`_resolve_cert_paths` seam.

    When auto-login is active (``auto_login_command`` set AND not
    ``--manual``), the user cannot interact with the terminal — auth is
    running unattended (typical ``--force`` from a cron job or similar) —
    so ``UserInputHandler`` is **excluded** and the race is the local
    server solo (an :class:`AutoLoginSupervisor` drives webauto outside
    the race; see :func:`_maybe_start_supervisor`).

    When auto-login is NOT active (no command, or ``--manual``), the race
    is the human-driven path: :class:`UserInputHandler` (paste fallback)
    plus the local callback server.
    """
    certfile, keyfile = _resolve_cert_paths(cfg.redirect_uri)
    try:
        local = LocalServerHandler(
            cfg.redirect_uri, certfile=certfile, keyfile=keyfile,
        )
    except CallbackServerError as e:
        # Port-in-use or TLS-load failure → actionable AuthFlowError instead of
        # a raw traceback surfacing through `schwab auth`.
        raise AuthFlowError(str(e)) from e

    auto_login_active = cfg.auto_login_command is not None and not manual
    if auto_login_active:
        return [local]
    return [UserInputHandler(), local]


def _maybe_start_supervisor(
    cfg: Config, *, manual: bool, auth_url: str, state: str,
) -> AutoLoginSupervisor | None:
    """Spawn the side-effect supervisor for the ``local_server`` flow with
    auto-login configured; otherwise return None.

    The supervisor keeps webauto-cli alive while the
    :class:`LocalServerHandler` captures the redirect. It never
    participates in the race itself.
    """
    if cfg.auto_login_command is None or manual:
        return None
    # Defensive only: ``get_auth_response`` already rejects legacy flows before
    # reaching here, so this guard is exercised only by direct callers/tests.
    if cfg.auth_flow not in AUTH_FLOWS:
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


def perform_full_auth(cfg: Config, *, manual: bool = False) -> "Session":
    """Run a full OAuth round-trip and persist the resulting session.

    Wraps the get_auth_response + resolve_auth_result + save_session
    pipeline that ``schwab auth --force`` and the scheduler's auth
    bridge both need. Returns the freshly-saved :class:`Session`.

    Headless-mode note: when the scheduler invokes this, it expects
    the configured ``auto_login_command`` (typically webauto-cli) to
    run headless. webauto defaults to ``HEADLESS=1`` so this works
    out-of-the-box; if you override to a visible browser in your
    webauto env file, scheduler-time auto-logins will also try to
    open a window — your call.

    Raises:
        AuthFlowError: every auth handler failed.
        AuthHandlerError: a handler-specific transport failure.
        oauth.OAuthAuthorizationError: Schwab returned an OAuth error.
        oauth.OAuthError / httpx errors: token exchange failed.
    """
    import time
    from schwab_cli import oauth
    from schwab_cli.session import Session, save as save_session

    result = get_auth_response(cfg, manual=manual)
    tr = oauth.resolve_auth_result(cfg, result)
    new_session = Session.from_token_response(tr, now=int(time.time()))
    save_session(new_session)
    return new_session
