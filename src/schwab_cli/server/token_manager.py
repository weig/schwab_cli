"""Daemon-side single owner of the OAuth token pair.

The :class:`TokenManager` runs two independent tracks (one thread each in
the server; see Phase-2 wiring) that together keep ``session.json``
perpetually valid:

* **Access track** (:meth:`TokenManager.access_step`) — proactively
  exchanges the refresh token for a new access token once the current one
  has burned half its lifetime. Transient failures (network, Schwab 5xx)
  back off to the quarter-life mark, then retry every minute until a
  token mints. ``invalid_grant`` means the refresh token is dead: the
  track hands off to the refresh track and stops exchanging until the
  session is replaced.
* **Refresh track** (:meth:`TokenManager.refresh_step`) — renews the
  refresh token itself via full (browser) auth at geometric checkpoints:
  1/2, 1/4, 1/8, ... of its lifetime remaining, single attempt per
  checkpoint, stopping at the 8h floor with a critical notification.
  It also services recovery requests (``invalid_grant`` handoffs) with
  an immediate full auth retried on exponential backoff
  (1m, 2m, 4m, ... capped at 3h, plus jitter) until it succeeds.

Both kinds of full auth run on the refresh track's thread, so they can
never race each other. On-demand consumers (a 401-handling client, the
``/auth/refresh`` endpoint) call :meth:`TokenManager.force_exchange`,
which is single-flight: concurrent callers share one Schwab round-trip.

All scheduling derives from the persisted session (``expires_at``,
``access_token_lifetime_s``, ``refresh_token_expires_at``), so a daemon
restart resumes exactly where the schedule left off — including
immediately attempting a checkpoint the downtime skipped.

Every effect (clock, OAuth I/O, full auth, session I/O, notifications,
sleeping, jitter) is injectable for tests.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

import httpx

import schwab_cli.auth_flows as auth_flows
import schwab_cli.session as session_module
from schwab_cli import oauth
from schwab_cli.session import REFRESH_TOKEN_LIFETIME_SECONDS, Session

if TYPE_CHECKING:
    from schwab_cli.config import Config
    from schwab_cli.oauth import TokenResponse

ErrorClass = Literal["invalid_grant", "transient"]
_ExchangeOutcome = tuple[Literal["success", "invalid_grant", "transient"], Session | None]


@dataclass(frozen=True)
class TokenPolicy:
    """Timing knobs for both tracks. Defaults implement the agreed spec."""

    # Access track: minute-retry cadence once inside the quarter-life window.
    access_retry_interval_s: int = 60
    # Recovery (invalid_grant) backoff: base doubles per failure up to cap,
    # with an independent additive jitter on every sleep.
    recovery_backoff_base_s: int = 60
    recovery_backoff_cap_s: int = 3 * 3600
    recovery_jitter_max_s: int = 30
    # Emit auth.recovery_failing from this consecutive-failure count on.
    recovery_notify_after: int = 3
    # Refresh track: no proactive attempts once remaining life is inside
    # this floor — critical-alert the user instead.
    refresh_stop_s: int = 8 * 3600
    refresh_lifetime_s: int = REFRESH_TOKEN_LIFETIME_SECONDS


def refresh_checkpoints(policy: TokenPolicy) -> list[int]:
    """Remaining-lifetime thresholds for proactive full auth, descending.

    Halve the lifetime until the floor: 7d/8h → [84h, 42h, 21h, 10.5h].
    """
    cps: list[int] = []
    c = policy.refresh_lifetime_s // 2
    while c > policy.refresh_stop_s:
        cps.append(c)
        c //= 2
    return cps


def classify_exchange_error(exc: BaseException) -> ErrorClass:
    """Sort a token-exchange failure into the two recovery paths.

    HTTP 400/401 mean Schwab rejected the grant itself (dead/revoked
    refresh token, malformed grant) — retrying the exchange can never
    succeed, so these route to full re-auth. Everything else (5xx, 429,
    network errors, unparseable responses) is transient: the same
    exchange is worth retrying.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (400, 401):
            return "invalid_grant"
        return "transient"
    return "transient"


def recovery_backoff_s(
    attempt: int, policy: TokenPolicy, rng: Callable[[], float],
) -> float:
    """Sleep before recovery attempt ``attempt + 1`` (1-based failures).

    Doubles from the base, capped, with additive jitter sampled fresh per
    call — the jitter never compounds into the doubling.
    """
    base = min(
        policy.recovery_backoff_base_s * (2 ** (attempt - 1)),
        policy.recovery_backoff_cap_s,
    )
    return base + rng() * policy.recovery_jitter_max_s


class TokenManager:
    """Owns all token writes for the daemon. See module docstring."""

    def __init__(
        self,
        cfg: "Config",
        *,
        policy: TokenPolicy | None = None,
        now: Callable[[], int] | None = None,
        emit: Callable[..., None] | None = None,
        exchange: Callable[["Config", str], "TokenResponse"] | None = None,
        full_auth: Callable[["Config"], Session] | None = None,
        load_session: Callable[[], Session | None] | None = None,
        save_session: Callable[[Session], None] | None = None,
        rng: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        on_session_replaced: Callable[[Session], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._policy = policy or TokenPolicy()
        self._now = now or (lambda: int(time.time()))
        self._emit = emit or (lambda event, **fields: None)
        self._exchange = exchange or oauth.refresh
        self._full_auth = full_auth or auth_flows.perform_full_auth
        self._load = load_session or session_module.load
        self._save = save_session or session_module.save
        self._rng = rng or random.random
        self._sleep_override = sleep
        self._on_session_replaced = on_session_replaced or (lambda s: None)

        # Guards the cross-thread notification latches (_lapse_notified,
        # _manual_block_token): both tracks' threads mutate them.
        self._state_lock = threading.Lock()

        # Access-track state.
        self._lapse_notified = False
        self._xch_lock = threading.Lock()
        self._xch_inflight: threading.Event | None = None
        self._xch_result: _ExchangeOutcome = ("transient", None)

        # Refresh-track state.
        self._recovery = threading.Event()
        self._recovery_reason = ""
        self._refresh_wake = threading.Event()
        self._cp_attempted: set[int] = set()
        self._cp_token_key: tuple[str, int] | None = None
        self._critical_for: int | None = None
        self._manual_block_token: str | None = None

    # ------------------------------------------------------------------
    # Access track
    # ------------------------------------------------------------------

    def access_step(self) -> int:
        """Do whatever the access track owes right now; return seconds
        until it should run again."""
        pol = self._policy
        s = self._load()
        if s is None:
            return pol.access_retry_interval_s
        if self._recovery.is_set() or self._manual_block_token == s.refresh_token:
            # The refresh token is known-dead; exchanging is pointless
            # until the refresh track (or the user) replaces the session.
            return pol.access_retry_interval_s

        now = self._now()
        half_at = s.expires_at - s.access_token_lifetime_s // 2
        if now < half_at:
            return half_at - now

        status, fresh = self._exchange_single_flight(s)
        if status == "success" and fresh is not None:
            next_half = fresh.expires_at - fresh.access_token_lifetime_s // 2
            return max(1, next_half - self._now())
        if status == "invalid_grant":
            self.request_recovery("invalid_grant during access-token exchange")
            return pol.access_retry_interval_s

        # Transient failure: hold until the quarter mark, then every minute.
        quarter_at = s.expires_at - s.access_token_lifetime_s // 4
        if now < quarter_at:
            return quarter_at - now
        if now >= s.expires_at:
            with self._state_lock:
                should_emit = not self._lapse_notified
                self._lapse_notified = True
            if should_emit:
                self._emit("auth.access_token_lapsed", expired_at=s.expires_at)
        return pol.access_retry_interval_s

    def access_loop(self, stop: threading.Event) -> None:
        """Drive :meth:`access_step` until ``stop`` is set."""
        while not stop.is_set():
            delay = self.access_step()
            # Cap the nap: the refresh track may replace the session
            # underneath us, which moves the half-life target.
            stop.wait(min(delay, 300))

    def force_exchange(self) -> Session | None:
        """One immediate single-flight exchange (the 401 / endpoint path).

        Returns the fresh session, or ``None`` when no session exists,
        the refresh token is known-dead (recovery already underway), or
        the exchange failed. ``invalid_grant`` additionally kicks the
        recovery track; the caller gets the failure immediately rather
        than blocking on a browser flow.
        """
        s = self._load()
        if s is None:
            return None
        if self._recovery.is_set() or self._manual_block_token == s.refresh_token:
            return None
        status, fresh = self._exchange_single_flight(s)
        if status == "success":
            return fresh
        if status == "invalid_grant":
            self.request_recovery("invalid_grant on forced exchange")
        return None

    def _exchange_single_flight(self, s: Session) -> _ExchangeOutcome:
        """Run one exchange, collapsing concurrent callers onto it."""
        ev: threading.Event | None = None
        with self._xch_lock:
            waiter = self._xch_inflight
            if waiter is None:
                ev = threading.Event()
                self._xch_inflight = ev

        if ev is None:
            # Follower: piggyback on the in-flight exchange. The 60s bound
            # only trips if the leader is stuck well past httpx's own 30s
            # timeout; report an honest transient failure rather than a
            # stale result from a previous flight.
            assert waiter is not None
            completed = waiter.wait(timeout=60.0)
            with self._xch_lock:
                return self._xch_result if completed else ("transient", None)

        result: _ExchangeOutcome = ("transient", None)
        try:
            result = self._do_exchange(s)
        finally:
            # Publish result, clear the flight, and wake waiters under one
            # lock so no new leader can start before waiters are signalled.
            with self._xch_lock:
                self._xch_result = result
                self._xch_inflight = None
                ev.set()
        return result

    def _do_exchange(self, s: Session) -> _ExchangeOutcome:
        try:
            tr = self._exchange(self._cfg, s.refresh_token)
        except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
            return (classify_exchange_error(e), None)
        fresh = Session.refreshed_from(s, tr, now=self._now())
        self._save(fresh)
        with self._state_lock:
            self._lapse_notified = False
        self._on_session_replaced(fresh)
        return ("success", fresh)

    # ------------------------------------------------------------------
    # Refresh track
    # ------------------------------------------------------------------

    def request_recovery(self, reason: str) -> None:
        """Ask the refresh track for an immediate full auth (idempotent)."""
        self._recovery_reason = reason
        self._recovery.set()
        self._refresh_wake.set()

    @property
    def recovery_pending(self) -> bool:
        return self._recovery.is_set()

    def wake(self) -> None:
        """Interrupt the refresh loop's nap (used on shutdown)."""
        self._refresh_wake.set()

    def refresh_step(self, stop: threading.Event | None = None) -> int:
        """Service one refresh-track obligation; return seconds to nap.

        A pending recovery request takes priority and blocks this thread
        through the backoff loop — by design: every full auth happens
        here, so scheduled checkpoints can never race a recovery.
        """
        pol = self._policy
        if self._recovery.is_set():
            self._run_recovery(stop)
            return 1

        s = self._load()
        if s is None:
            return 3600
        self._reset_checkpoint_state_if_token_changed(s)

        remaining = s.refresh_token_expires_at - self._now()
        if remaining <= pol.refresh_stop_s:
            if self._critical_for != s.refresh_token_expires_at:
                self._critical_for = s.refresh_token_expires_at
                self._emit(
                    "auth.refresh_token_critical",
                    remaining_h=max(0, remaining) // 3600,
                    action="run `schwab auth --force`",
                )
            return 3600

        if self._cfg.auto_login_command is None:
            # Rule 5: unattended renewal impossible — sleep toward the
            # critical alert; no checkpoint attempts.
            return max(1, remaining - pol.refresh_stop_s)

        cps = refresh_checkpoints(pol)
        due = [c for c in cps if remaining <= c and c not in self._cp_attempted]
        if due:
            # One attempt covers every threshold we are already past —
            # a restart that slept through a checkpoint retries once, not
            # once per missed threshold.
            self._cp_attempted.update(c for c in cps if remaining <= c)
            try:
                fresh = self._full_auth(self._cfg)
            except Exception as e:  # noqa: BLE001 — any failure waits for the next checkpoint
                self._emit(
                    "auth.renewal_attempt_failed",
                    checkpoint_h=max(due) // 3600,
                    remaining_h=remaining // 3600,
                    error=f"{type(e).__name__}: {e}",
                )
            else:
                self._after_full_auth_success(fresh)
                self._emit(
                    "auth.auto_login.succeeded",
                    trigger="renewal checkpoint",
                )
                return self._nap_until_next_checkpoint(fresh)

        lower = [c for c in cps if c < remaining and c not in self._cp_attempted]
        target = max(lower) if lower else pol.refresh_stop_s
        return max(1, remaining - target)

    def refresh_loop(self, stop: threading.Event) -> None:
        """Drive :meth:`refresh_step` until ``stop`` is set; recovery
        requests interrupt the nap via :meth:`request_recovery`."""
        while not stop.is_set():
            delay = self.refresh_step(stop)
            self._refresh_wake.wait(timeout=min(delay, 3600))
            self._refresh_wake.clear()

    def _run_recovery(self, stop: threading.Event | None) -> None:
        pol = self._policy
        if self._cfg.auto_login_command is None:
            # Rule 5: nothing to retry unattended — tell the user once
            # per dead token and lift the block only when it changes.
            s = self._load()
            token = s.refresh_token if s is not None else ""
            with self._state_lock:
                should_emit = self._manual_block_token != token
                self._manual_block_token = token
            if should_emit:
                self._emit(
                    "auth.manual_auth_required", reason=self._recovery_reason,
                )
            self._recovery.clear()
            return

        attempt = 0
        while True:
            if stop is not None and stop.is_set():
                return
            attempt += 1
            try:
                fresh = self._full_auth(self._cfg)
            except Exception as e:  # noqa: BLE001 — every failure feeds the backoff
                if attempt >= pol.recovery_notify_after:
                    self._emit(
                        "auth.recovery_failing",
                        attempt=attempt,
                        error=f"{type(e).__name__}: {e}",
                    )
                self._nap(recovery_backoff_s(attempt, pol, self._rng), stop)
                continue
            self._after_full_auth_success(fresh)
            self._emit("auth.recovery_succeeded", attempts=attempt)
            self._recovery.clear()
            return

    def _after_full_auth_success(self, fresh: Session) -> None:
        # perform_full_auth persists the session itself, but TokenManager is
        # the daemon's single token owner — re-save defensively (idempotent)
        # so a custom full_auth that forgets to persist still ends durable.
        self._save(fresh)
        with self._state_lock:
            self._lapse_notified = False
            self._manual_block_token = None
        self._critical_for = None
        self._cp_token_key = (fresh.refresh_token, fresh.refresh_token_expires_at)
        self._cp_attempted = set()
        self._on_session_replaced(fresh)

    def _reset_checkpoint_state_if_token_changed(self, s: Session) -> None:
        key = (s.refresh_token, s.refresh_token_expires_at)
        if key != self._cp_token_key:
            self._cp_token_key = key
            self._cp_attempted = set()

    def _nap_until_next_checkpoint(self, fresh: Session) -> int:
        remaining = fresh.refresh_token_expires_at - self._now()
        cps = refresh_checkpoints(self._policy)
        lower = [c for c in cps if c < remaining]
        target = max(lower) if lower else self._policy.refresh_stop_s
        return max(1, remaining - target)

    def _nap(self, seconds: float, stop: threading.Event | None) -> None:
        if self._sleep_override is not None:
            self._sleep_override(seconds)
        elif stop is not None:
            stop.wait(seconds)
        else:
            time.sleep(seconds)

    # ------------------------------------------------------------------
    # Introspection (Phase-2 /auth/status endpoint)
    # ------------------------------------------------------------------

    def state(self) -> dict:
        s = self._load()
        return {
            "access_expires_at": s.expires_at if s is not None else None,
            "access_token_lifetime_s": (
                s.access_token_lifetime_s if s is not None else None
            ),
            "refresh_token_expires_at": (
                s.refresh_token_expires_at if s is not None else None
            ),
            "recovery_pending": self._recovery.is_set(),
            "manual_auth_required": bool(
                s is not None and self._manual_block_token == s.refresh_token
            ),
            "auto_login_enabled": self._cfg.auto_login_command is not None,
        }
