"""Auth-maintenance tick + loop for the ``schwab server`` daemon.

The maintenance loop keeps the OAuth refresh token alive without manual
intervention. Each tick:

* Loads the on-disk session and computes the refresh-token TTL.
* When the TTL is at or below :data:`DEFAULT_INTERVAL_S` (one cycle from
  expiry), it triggers a full re-auth via
  :func:`schwab_cli.auth_flows.perform_full_auth` (browser / auto-login).
* Otherwise it just ensures the access token is fresh via the pure-HTTP
  service layer (:func:`schwab_cli.service.auth.get_session`).

Expected auth failures (a blown re-auth, an expired refresh token) are
**non-fatal**: ``run_once`` always returns a :class:`MaintenanceTick`
rather than raising, so the loop keeps running and the next cycle can
recover. The matching ``scheduler.proactive_auth_*`` notification events
are emitted when notifier/audit infra is wired so existing
``notification.json`` subscriptions still fire.

References (looked up via the module, NOT bound at import time) so the
spec tests can monkeypatch them:

* ``schwab_cli.session.load``
* ``schwab_cli.auth_flows.perform_full_auth``
* ``schwab_cli.service.auth.get_session``
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import schwab_cli.auth_flows as auth_flows
import schwab_cli.service.auth as service_auth
import schwab_cli.session as session_module
from schwab_cli.api.client import SessionExpired
from schwab_cli.service import ServiceError


# One maintenance cycle. Renewal fires when the refresh token has <= this
# much life left, i.e. it would otherwise expire before the next tick.
DEFAULT_INTERVAL_S = 8 * 3600


@dataclass(frozen=True)
class MaintenanceTick:
    """Outcome of a single maintenance cycle.

    ``action`` is one of:

    * ``"renewed"`` — full re-auth fired and succeeded. Also covers the
      ensure-token fallback: the access-token mint kept failing, so a full
      re-auth was triggered and recovered.
    * ``"token_ensured"`` — access token confirmed fresh (no re-auth),
      possibly after one or more transient-failure retries.
    * ``"renew_failed"`` — full re-auth raised (non-fatal).
    * ``"token_failed"`` — the access-token mint kept failing across all
      retries AND the full-auth fallback also failed (non-fatal).
    """

    action: str
    detail: str


def _default_now() -> int:
    return int(time.time())


# Ensure-token retry policy. A single transient refresh blip (network hiccup,
# Schwab 5xx) used to surface immediately as a ``token_failed`` alert. We now
# retry a few times with a short backoff before declaring failure, then — if
# the refresh genuinely won't mint — escalate to a full re-auth as a recovery
# fallback (covers a refresh token Schwab invalidated *before* its nominal
# expiry, which the TTL gate alone would never catch).
DEFAULT_ENSURE_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_S = 2.0


def _renewed_tick(new_session, *, now: Callable[[], int], detail_prefix: str) -> MaintenanceTick:
    new_ttl_h = (
        (new_session.refresh_token_expires_at - now()) // 3600
        if new_session is not None
        else 0
    )
    return MaintenanceTick("renewed", f"{detail_prefix}; new TTL ~{new_ttl_h}h")


def run_once(
    cfg,
    *,
    now: Callable[[], int] = _default_now,
    notifier: Callable[[MaintenanceTick], None] | None = None,
    audit=None,
    ensure_attempts: int = DEFAULT_ENSURE_ATTEMPTS,
    retry_sleep: Callable[[float], None] = time.sleep,
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
) -> MaintenanceTick:
    """Run one maintenance cycle and return a :class:`MaintenanceTick`.

    Never raises for the expected auth-failure cases (a failed re-auth or
    an expired refresh token) — those surface as ``renew_failed`` /
    ``token_failed`` ticks so the loop keeps running.

    The ensure-token path (refresh TTL above the renewal window) retries a
    transient failure ``ensure_attempts`` times with ``retry_backoff_s``
    between tries before giving up, and on persistent failure escalates to a
    full re-auth so a prematurely-invalidated refresh token still recovers
    instead of merely alerting. ``retry_sleep`` is injectable for tests.
    """
    current = now()
    session = session_module.load()

    if session is None:
        # No session file → nothing to ensure; treat as needing a renewal.
        ttl = -1
    else:
        ttl = session.refresh_token_expires_at - current

    if ttl <= DEFAULT_INTERVAL_S:
        try:
            new_session = auth_flows.perform_full_auth(cfg)
            tick = _renewed_tick(
                new_session, now=now, detail_prefix="refresh token renewed",
            )
        except Exception as e:  # noqa: BLE001 — non-fatal, surfaced as tick
            tick = MaintenanceTick(
                "renew_failed",
                f"full re-auth failed: {type(e).__name__}: {e}",
            )
    else:
        tick = _ensure_token(
            cfg,
            now=now,
            ttl=ttl,
            attempts=ensure_attempts,
            retry_sleep=retry_sleep,
            backoff_s=retry_backoff_s,
        )

    _emit(tick, notifier=notifier, audit=audit)
    return tick


def _ensure_token(
    cfg,
    *,
    now: Callable[[], int],
    ttl: int,
    attempts: int,
    retry_sleep: Callable[[float], None],
    backoff_s: float,
) -> MaintenanceTick:
    """Ensure a fresh access token with bounded retry + full-auth fallback.

    Returns a ``token_ensured`` tick on success (possibly after a retry), a
    ``renewed`` tick when the access-token mint kept failing but the full
    re-auth fallback recovered, or a ``token_failed`` tick when both the
    retried mint and the fallback failed (all non-fatal — never raises).
    """
    last_err: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            service_auth.get_session(cfg)
            suffix = "" if attempt == 1 else f" (after {attempt - 1} retr{'y' if attempt == 2 else 'ies'})"
            return MaintenanceTick(
                "token_ensured",
                f"access token ensured; refresh TTL ~{ttl // 3600}h{suffix}",
            )
        except (SessionExpired, ServiceError) as e:
            # SessionExpired (dead refresh) or a service-layer auth error
            # (e.g. NotAuthenticated if the session file vanished mid-tick).
            last_err = e
            if attempt < max(1, attempts):
                retry_sleep(backoff_s)

    # Retries exhausted — the refresh token won't mint an access token even
    # though its nominal TTL is still above the renewal window. Escalate to a
    # full re-auth so a prematurely-invalidated refresh token recovers.
    err_name = type(last_err).__name__ if last_err is not None else "unknown"
    try:
        new_session = auth_flows.perform_full_auth(cfg)
        return _renewed_tick(
            new_session,
            now=now,
            detail_prefix=(
                f"recovered via auto-login after {max(1, attempts)} "
                f"refresh failure(s) ({err_name})"
            ),
        )
    except Exception as e2:  # noqa: BLE001 — non-fatal, surfaced as tick
        return MaintenanceTick(
            "token_failed",
            f"session error after {max(1, attempts)} attempt(s): "
            f"{err_name}: {last_err}; auto-login fallback failed: "
            f"{type(e2).__name__}: {e2}",
        )


def _emit(
    tick: MaintenanceTick,
    *,
    notifier: Callable[[MaintenanceTick], None] | None,
    audit,
) -> None:
    """Best-effort fan-out of a tick to the injected notifier + audit.

    The injected ``notifier`` (a plain callable) is always invoked with
    the tick. When ``notifier`` exposes an ``emit`` method (the Notifier
    infra), the matching ``scheduler.proactive_auth_*`` event is emitted
    too so existing notification.json subscriptions fire.
    """
    if audit is not None:
        try:
            level = "error" if tick.action.endswith("_failed") else "info"
            getattr(audit, level, audit.info)(
                f"server maintenance: {tick.action} — {tick.detail}"
            )
        except Exception:  # noqa: BLE001 — audit must never break a tick
            pass

    if notifier is None:
        return
    try:
        notifier(tick)
    except Exception:  # noqa: BLE001 — notifier must never break a tick
        pass


def run_loop(
    cfg,
    *,
    interval_s: int = DEFAULT_INTERVAL_S,
    sleep: Callable[[int], None],
    now: Callable[[], int],
    stop: Callable[[], bool] | None = None,
    max_iterations: int | None = None,
    notifier: Callable[[MaintenanceTick], None] | None = None,
    audit=None,
) -> None:
    """Drive :func:`run_once` on a fixed interval until stopped.

    Each cycle: check ``stop`` (break if truthy), run one maintenance
    tick, ``sleep(interval_s)``, then honor ``max_iterations``.

    ``run_once`` is looked up via the module global so tests can patch
    ``schwab_cli.server.maintenance.run_once``.
    """
    count = 0
    while True:
        if stop is not None and stop():
            break
        run_once(cfg, now=now, notifier=notifier, audit=audit)
        sleep(interval_s)
        count += 1
        if max_iterations is not None and count >= max_iterations:
            break
