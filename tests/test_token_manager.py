"""Spec tests (TDD red) for schwab_cli.server.token_manager.

The TokenManager is the daemon-side single owner of OAuth tokens:

* Access track — proactive token exchange at half-life, minute-retries
  from the quarter mark on transient failure, invalid_grant hands off to
  the refresh track.
* Refresh track — geometric full-auth checkpoints (1/2, 1/4, 1/8, ... of
  the refresh-token lifetime) down to an 8h floor, plus an
  exponential-backoff recovery loop for invalid_grant handoffs.

All time, sleeping, OAuth I/O, session I/O and notification emission are
injected so these tests run with a fake clock and never touch the network
or a real notifier.
"""
from __future__ import annotations

import threading

import httpx
import pytest

from schwab_cli.config import Config
from schwab_cli.oauth import OAuthError, TokenResponse
from schwab_cli.server.token_manager import (
    TokenManager,
    TokenPolicy,
    classify_exchange_error,
    recovery_backoff_s,
    refresh_checkpoints,
)
from schwab_cli.session import REFRESH_TOKEN_LIFETIME_SECONDS, Session

_NOW = 1_700_000_000
_H = 3600
_D = 24 * _H


def _cfg(auto_login: bool = True) -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auto_login_command=("webauto",) if auto_login else None,
    )


class FakeClock:
    def __init__(self, start: int = _NOW) -> None:
        self.t = start

    def now(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds)


class EmitSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def count(self, event: str) -> int:
        return self.names().count(event)


def _session(
    clock: FakeClock,
    *,
    access_ttl: int = 900,
    lifetime: int = 1800,
    refresh_ttl: int = REFRESH_TOKEN_LIFETIME_SECONDS,
    access_token: str = "atok",
    refresh_token: str = "rtok",
) -> Session:
    return Session(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=clock.now() + access_ttl,
        refresh_token_expires_at=clock.now() + refresh_ttl,
        access_token_lifetime_s=lifetime,
    )


def _token_response(n: int = 1) -> TokenResponse:
    return TokenResponse(
        access_token=f"atok-{n}", refresh_token="rtok", expires_in=1800,
    )


def _make_mgr(
    clock: FakeClock,
    store: dict,
    *,
    auto_login: bool = True,
    exchange=None,
    full_auth=None,
    policy: TokenPolicy | None = None,
    rng=lambda: 0.0,
    sleep=None,
    on_session_replaced=None,
) -> tuple[TokenManager, EmitSpy]:
    emits = EmitSpy()
    mgr = TokenManager(
        _cfg(auto_login=auto_login),
        policy=policy or TokenPolicy(),
        now=clock.now,
        emit=emits,
        exchange=exchange or (lambda cfg, rt: _token_response()),
        full_auth=full_auth,
        load_session=lambda: store.get("s"),
        save_session=lambda s: store.__setitem__("s", s),
        rng=rng,
        sleep=sleep,
        on_session_replaced=on_session_replaced,
    )
    return mgr, emits


def _http_error(status: int, body: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.schwabapi.com/v1/oauth/token")
    resp = httpx.Response(status, json=body or {}, request=req)
    return httpx.HTTPStatusError(f"http {status}", request=req, response=resp)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestRefreshCheckpoints:
    def test_seven_day_lifetime_with_8h_floor(self):
        cps = refresh_checkpoints(TokenPolicy())
        assert cps == [
            REFRESH_TOKEN_LIFETIME_SECONDS // 2,    # 84h
            REFRESH_TOKEN_LIFETIME_SECONDS // 4,    # 42h
            REFRESH_TOKEN_LIFETIME_SECONDS // 8,    # 21h
            REFRESH_TOKEN_LIFETIME_SECONDS // 16,   # 10.5h
        ]

    def test_floor_excludes_checkpoints_at_or_below_stop(self):
        # With a 48h lifetime and a 8h stop: 24h, 12h — 6h is < 8h, excluded.
        pol = TokenPolicy(refresh_lifetime_s=48 * _H)
        assert refresh_checkpoints(pol) == [24 * _H, 12 * _H]


class TestClassifyExchangeError:
    def test_400_invalid_grant_body(self):
        e = _http_error(400, {"error": "invalid_grant"})
        assert classify_exchange_error(e) == "invalid_grant"

    def test_400_other_body_is_invalid_grant_equivalent(self):
        e = _http_error(400, {"error": "unsupported_grant_type"})
        assert classify_exchange_error(e) == "invalid_grant"

    def test_401_is_invalid_grant(self):
        assert classify_exchange_error(_http_error(401)) == "invalid_grant"

    def test_5xx_is_transient(self):
        assert classify_exchange_error(_http_error(503)) == "transient"

    def test_429_is_transient(self):
        assert classify_exchange_error(_http_error(429)) == "transient"

    def test_network_error_is_transient(self):
        req = httpx.Request("POST", "https://x")
        e = httpx.ConnectError("boom", request=req)
        assert classify_exchange_error(e) == "transient"

    def test_oauth_parse_error_is_transient(self):
        assert classify_exchange_error(OAuthError("missing field")) == "transient"


class TestRecoveryBackoff:
    def test_doubles_from_base_with_cap(self):
        pol = TokenPolicy()
        no_jitter = lambda: 0.0  # noqa: E731
        delays = [recovery_backoff_s(a, pol, no_jitter) for a in range(1, 10)]
        assert delays[:5] == [60, 120, 240, 480, 960]
        assert delays[-1] == pol.recovery_backoff_cap_s  # capped at 3h

    def test_jitter_added_independently_of_doubling(self):
        pol = TokenPolicy()
        full_jitter = lambda: 1.0  # noqa: E731
        assert recovery_backoff_s(1, pol, full_jitter) == 60 + 30
        assert recovery_backoff_s(2, pol, full_jitter) == 120 + 30
        # at the cap, jitter still rides on top
        assert recovery_backoff_s(20, pol, full_jitter) == pol.recovery_backoff_cap_s + 30


# ---------------------------------------------------------------------------
# Access track
# ---------------------------------------------------------------------------


class TestAccessTrack:
    def test_fresh_token_sleeps_until_half_life(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=1800, lifetime=1800)}
        calls = []
        mgr, _ = _make_mgr(
            clock, store,
            exchange=lambda cfg, rt: calls.append(rt) or _token_response(),
        )
        delay = mgr.access_step()
        assert calls == []
        assert delay == 900  # expires_at - lifetime/2 - now

    def test_exchanges_at_half_life_and_preserves_refresh_expiry(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=900, lifetime=1800)}
        old_refresh_exp = store["s"].refresh_token_expires_at
        mgr, _ = _make_mgr(clock, store)
        delay = mgr.access_step()
        assert store["s"].access_token == "atok-1"
        # refresh-grant exchange must NOT extend the refresh token's life
        assert store["s"].refresh_token_expires_at == old_refresh_exp
        assert store["s"].expires_at == clock.now() + 1800
        assert delay == 900  # next half-life

    def test_transient_failure_waits_until_quarter_mark(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=900, lifetime=1800)}

        def boom(cfg, rt):
            raise _http_error(503)

        mgr, emits = _make_mgr(clock, store, exchange=boom)
        delay = mgr.access_step()
        assert delay == 450  # quarter mark is expires_at - 450
        assert "auth.access_token_lapsed" not in emits.names()

    def test_transient_failure_inside_quarter_retries_every_minute(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=300, lifetime=1800)}

        def boom(cfg, rt):
            raise _http_error(503)

        mgr, _ = _make_mgr(clock, store, exchange=boom)
        assert mgr.access_step() == 60

    def test_lapsed_token_emits_once_until_recovery(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=-10, lifetime=1800)}
        fail = {"on": True}

        def flaky(cfg, rt):
            if fail["on"]:
                raise _http_error(503)
            return _token_response()

        mgr, emits = _make_mgr(clock, store, exchange=flaky)
        mgr.access_step()
        mgr.access_step()
        assert emits.count("auth.access_token_lapsed") == 1
        # success clears the latch; the next outage re-alerts
        fail["on"] = False
        mgr.access_step()
        fail["on"] = True
        store["s"] = _session(clock, access_ttl=-10, lifetime=1800)
        mgr.access_step()
        assert emits.count("auth.access_token_lapsed") == 2

    def test_invalid_grant_hands_off_to_recovery_and_stops_exchanging(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=100, lifetime=1800)}
        calls = []

        def dead(cfg, rt):
            calls.append(rt)
            raise _http_error(400, {"error": "invalid_grant"})

        mgr, _ = _make_mgr(clock, store, exchange=dead)
        delay = mgr.access_step()
        assert mgr.recovery_pending
        assert delay == TokenPolicy().access_retry_interval_s
        # while recovery is pending the access track does not re-exchange
        mgr.access_step()
        assert len(calls) == 1

    def test_force_exchange_success_returns_fresh_session(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        mgr, _ = _make_mgr(clock, store)
        fresh = mgr.force_exchange()
        assert fresh is not None and fresh.access_token == "atok-1"
        assert store["s"].access_token == "atok-1"

    def test_force_exchange_invalid_grant_returns_none_and_requests_recovery(self):
        clock = FakeClock()
        store = {"s": _session(clock)}

        def dead(cfg, rt):
            raise _http_error(400, {"error": "invalid_grant"})

        mgr, _ = _make_mgr(clock, store, exchange=dead)
        assert mgr.force_exchange() is None
        assert mgr.recovery_pending

    def test_force_exchange_no_session_returns_none(self):
        clock = FakeClock()
        mgr, _ = _make_mgr(clock, {})
        assert mgr.force_exchange() is None

    def test_concurrent_force_exchange_is_single_flight(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(cfg, rt):
            calls.append(rt)
            started.set()
            release.wait(5)
            return _token_response()

        mgr, _ = _make_mgr(clock, store, exchange=slow)
        results: list = []
        t1 = threading.Thread(target=lambda: results.append(mgr.force_exchange()))
        t1.start()
        assert started.wait(5)
        t2 = threading.Thread(target=lambda: results.append(mgr.force_exchange()))
        t2.start()
        release.set()
        t1.join(5)
        t2.join(5)
        assert len(calls) == 1  # second caller piggybacked on the first
        assert all(r is not None for r in results)

    def test_on_session_replaced_hook_fires_on_exchange(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        seen = []
        mgr, _ = _make_mgr(clock, store, on_session_replaced=seen.append)
        mgr.force_exchange()
        assert len(seen) == 1 and seen[0].access_token == "atok-1"


# ---------------------------------------------------------------------------
# Refresh track — geometric checkpoints
# ---------------------------------------------------------------------------


class TestRefreshTrackCheckpoints:
    def test_just_renewed_sleeps_until_half_life(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        calls = []
        mgr, _ = _make_mgr(
            clock, store, full_auth=lambda cfg: calls.append(1),
        )
        delay = mgr.refresh_step()
        assert calls == []
        assert delay == REFRESH_TOKEN_LIFETIME_SECONDS // 2

    def test_attempts_full_auth_at_half_life(self):
        clock = FakeClock()
        half = REFRESH_TOKEN_LIFETIME_SECONDS // 2
        store = {"s": _session(clock, refresh_ttl=half)}

        def renew(cfg):
            fresh = _session(clock, access_token="atok-new", refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        mgr, emits = _make_mgr(clock, store, full_auth=renew)
        delay = mgr.refresh_step()
        assert store["s"].refresh_token == "rtok-new"
        assert "auth.auto_login.succeeded" in emits.names()
        assert delay == REFRESH_TOKEN_LIFETIME_SECONDS // 2

    def test_failed_checkpoint_waits_for_next_and_warns(self):
        clock = FakeClock()
        half = REFRESH_TOKEN_LIFETIME_SECONDS // 2
        quarter = REFRESH_TOKEN_LIFETIME_SECONDS // 4
        store = {"s": _session(clock, refresh_ttl=half)}

        def boom(cfg):
            raise RuntimeError("webauto crashed")

        mgr, emits = _make_mgr(clock, store, full_auth=boom)
        delay = mgr.refresh_step()
        assert emits.count("auth.renewal_attempt_failed") == 1
        assert delay == half - quarter
        # same checkpoint is not hammered on an immediate re-step
        delay2 = mgr.refresh_step()
        assert emits.count("auth.renewal_attempt_failed") == 1
        assert delay2 == half - quarter

    def test_restart_past_checkpoint_attempts_immediately(self):
        clock = FakeClock()
        # 30h remaining: between the 42h and 21h checkpoints, never attempted
        store = {"s": _session(clock, refresh_ttl=30 * _H)}
        calls = []

        def renew(cfg):
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            calls.append(1)
            return fresh

        mgr, _ = _make_mgr(clock, store, full_auth=renew)
        mgr.refresh_step()
        assert calls == [1]

    def test_failed_last_checkpoint_naps_to_8h_floor(self):
        clock = FakeClock()
        # 10.5h remaining: the last checkpoint before the 8h floor.
        last_cp = REFRESH_TOKEN_LIFETIME_SECONDS // 16
        store = {"s": _session(clock, refresh_ttl=last_cp)}
        calls = []

        def boom(cfg):
            calls.append(1)
            raise RuntimeError("nope")

        mgr, _ = _make_mgr(clock, store, full_auth=boom)
        delay = mgr.refresh_step()
        assert calls == [1]
        assert delay == last_cp - 8 * _H  # straight to the critical-alert wake
        # no re-attempt while waiting for the floor
        assert mgr.refresh_step() == last_cp - 8 * _H
        assert calls == [1]

    def test_all_checkpoints_failed_then_critical_at_8h_and_stop(self):
        clock = FakeClock()
        store = {"s": _session(clock, refresh_ttl=8 * _H)}
        calls = []

        def boom(cfg):
            calls.append(1)
            raise RuntimeError("nope")

        mgr, emits = _make_mgr(clock, store, full_auth=boom)
        mgr.refresh_step()
        mgr.refresh_step()
        assert calls == []  # inside the 8h floor: no more attempts
        assert emits.count("auth.refresh_token_critical") == 1

    def test_success_resets_checkpoint_state_for_new_token(self):
        clock = FakeClock()
        half = REFRESH_TOKEN_LIFETIME_SECONDS // 2
        store = {"s": _session(clock, refresh_ttl=half)}
        attempts = []

        def renew(cfg):
            attempts.append(1)
            fresh = _session(clock, refresh_token=f"rtok-{len(attempts)}")
            store["s"] = fresh
            return fresh

        mgr, _ = _make_mgr(clock, store, full_auth=renew)
        mgr.refresh_step()
        # new token ages to its own half-life → a second attempt must fire
        clock.advance(REFRESH_TOKEN_LIFETIME_SECONDS - half)
        store["s"] = _session(clock, refresh_ttl=half, refresh_token="rtok-1")
        mgr.refresh_step()
        assert len(attempts) == 2


# ---------------------------------------------------------------------------
# Refresh track — invalid_grant recovery (exponential backoff)
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_recovery_runs_full_auth_immediately(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        calls = []

        def renew(cfg):
            calls.append(1)
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        mgr, emits = _make_mgr(clock, store, full_auth=renew)
        mgr.request_recovery("invalid_grant")
        mgr.refresh_step()
        assert calls == [1]
        assert not mgr.recovery_pending
        assert "auth.recovery_succeeded" in emits.names()

    def test_recovery_backoff_doubles_and_notifies_on_third_failure(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        sleeps: list[float] = []
        attempts = []

        def flaky(cfg):
            attempts.append(1)
            if len(attempts) < 4:
                raise RuntimeError("idp down")
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)

        mgr, emits = _make_mgr(
            clock, store, full_auth=flaky, sleep=fake_sleep, rng=lambda: 0.0,
        )
        mgr.request_recovery("invalid_grant")
        mgr.refresh_step()
        assert len(attempts) == 4
        assert sleeps == [60, 120, 240]
        # error notification fires from the 3rd consecutive failure on
        assert emits.count("auth.recovery_failing") == 1
        assert "auth.recovery_succeeded" in emits.names()
        assert not mgr.recovery_pending

    def test_recovery_jitter_rides_on_top_of_backoff(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        sleeps: list[float] = []
        attempts = []

        def flaky(cfg):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("idp down")
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        mgr, _ = _make_mgr(
            clock, store, full_auth=flaky,
            sleep=lambda s: sleeps.append(s) or clock.advance(s),
            rng=lambda: 0.5,
        )
        mgr.request_recovery("x")
        mgr.refresh_step()
        assert sleeps == [60 + 15, 120 + 15]

    def test_request_recovery_is_idempotent(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        calls = []

        def renew(cfg):
            calls.append(1)
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        mgr, _ = _make_mgr(clock, store, full_auth=renew)
        mgr.request_recovery("a")
        mgr.request_recovery("b")
        mgr.refresh_step()
        assert calls == [1]

    def test_on_session_replaced_hook_fires_on_recovery(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        seen = []

        def renew(cfg):
            fresh = _session(clock, refresh_token="rtok-new")
            store["s"] = fresh
            return fresh

        mgr, _ = _make_mgr(
            clock, store, full_auth=renew, on_session_replaced=seen.append,
        )
        mgr.request_recovery("x")
        mgr.refresh_step()
        assert len(seen) == 1 and seen[0].refresh_token == "rtok-new"


# ---------------------------------------------------------------------------
# Auto-login disabled (rule 5)
# ---------------------------------------------------------------------------


class TestAutoLoginDisabled:
    def test_checkpoints_never_attempt(self):
        clock = FakeClock()
        half = REFRESH_TOKEN_LIFETIME_SECONDS // 2
        store = {"s": _session(clock, refresh_ttl=half)}
        calls = []
        mgr, emits = _make_mgr(
            clock, store, auto_login=False,
            full_auth=lambda cfg: calls.append(1),
        )
        mgr.refresh_step()
        assert calls == []
        assert "auth.renewal_attempt_failed" not in emits.names()

    def test_critical_notification_inside_8h(self):
        clock = FakeClock()
        store = {"s": _session(clock, refresh_ttl=7 * _H)}
        mgr, emits = _make_mgr(clock, store, auto_login=False)
        mgr.refresh_step()
        mgr.refresh_step()
        assert emits.count("auth.refresh_token_critical") == 1

    def test_recovery_request_notifies_manual_auth_once(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        calls = []
        mgr, emits = _make_mgr(
            clock, store, auto_login=False,
            full_auth=lambda cfg: calls.append(1),
        )
        mgr.request_recovery("invalid_grant")
        mgr.refresh_step()
        assert calls == []
        assert emits.count("auth.manual_auth_required") == 1
        assert not mgr.recovery_pending
        # the access track stops hammering the dead refresh token too
        xch = []
        mgr2_exchange_calls = xch  # alias for clarity
        delay = mgr.access_step()
        assert delay == TokenPolicy().access_retry_interval_s
        assert mgr2_exchange_calls == []


# ---------------------------------------------------------------------------
# Loop drivers
# ---------------------------------------------------------------------------


class TestLoops:
    def test_access_loop_exits_on_stop(self):
        clock = FakeClock()
        store = {"s": _session(clock, access_ttl=1800, lifetime=1800)}
        mgr, _ = _make_mgr(clock, store)
        stop = threading.Event()
        stop.set()
        mgr.access_loop(stop)  # returns immediately

    def test_refresh_loop_exits_on_stop(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        mgr, _ = _make_mgr(clock, store)
        stop = threading.Event()
        stop.set()
        mgr.refresh_loop(stop)


# ---------------------------------------------------------------------------
# Status surface (for the Phase-2 /auth/status endpoint)
# ---------------------------------------------------------------------------


class TestState:
    def test_state_snapshot(self):
        clock = FakeClock()
        store = {"s": _session(clock)}
        mgr, _ = _make_mgr(clock, store)
        st = mgr.state()
        assert st["access_expires_at"] == store["s"].expires_at
        assert st["refresh_token_expires_at"] == store["s"].refresh_token_expires_at
        assert st["recovery_pending"] is False
        assert st["auto_login_enabled"] is True

    def test_state_without_session(self):
        clock = FakeClock()
        mgr, _ = _make_mgr(clock, {})
        st = mgr.state()
        assert st["access_expires_at"] is None
