"""Proactive refresh-token rotation monitor.

Runs as a background asyncio task inside the HTTP daemon. Wakes up
near ``refresh_token_expires_at - threshold`` and spawns
``schwab_cli auth --force`` (with ``HEADLESS=1``) to rotate the
7-day refresh token before it dies. Notifier + logbook get
updates on each lifecycle beat.

Anti-thrash: at most one rotation attempt per hour even on
repeated failures. A successful rotation resets the thrash guard
immediately.

Defaults are tuned for the 7-day Schwab refresh-token lifetime:

* Threshold: **1h remaining** — still plenty of window to recover
  from a first-attempt failure before the token actually dies.
* Anti-thrash: **60 min** between attempts.
* Check cadence: wake every **60s** to catch the threshold
  crossing without wasting cycles.

The monitor does not drive the browser itself; it shells out to
the existing ``schwab_cli auth --force`` command so all the
selenium + chromium profile logic lives in one place.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Callable

from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.notify import Notifier
from schwab_cli.session import Session, load as load_session


DEFAULT_THRESHOLD_SECONDS = 3600          # 1 hour
DEFAULT_ANTI_THRASH_SECONDS = 3600        # 1 hour between attempts
DEFAULT_POLL_SECONDS = 60                 # tick every minute
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 330  # auto_login_timeout (300s) + 30s buffer
DEFAULT_REFRESH_EXPIRING_WARN_SECONDS = 900  # 15-minute danger zone


@dataclass
class AuthMonitorResult:
    """Structured outcome of one rotation attempt. Exposed for
    tests and for the ``reauth`` MCP tool handler."""

    ok: bool
    stderr_tail: str = ""
    duration_sec: float = 0.0


class AuthMonitor:
    """Background task that proactively rotates the Schwab refresh
    token. One instance per :class:`SchwabMcpServer`.

    Dependencies are injected to keep the class testable:

    * ``session_loader`` — ``() -> Session``; defaults to disk read.
    * ``subprocess_runner`` — ``(cmd, env) -> (returncode, stderr)``;
      defaults to ``asyncio.create_subprocess_exec``.
    * ``clock`` — ``() -> float``; defaults to ``time.time``.

    Hooks:

    * ``on_rotation_success`` — async callback fired after disk
      reload succeeds. The HTTP daemon uses this to reconnect the
      Schwab streamer with the new access token.
    """

    def __init__(
        self,
        logbook: LogBook,
        notifier: Notifier,
        *,
        enabled: bool = True,
        threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
        anti_thrash_seconds: int = DEFAULT_ANTI_THRASH_SECONDS,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        subprocess_timeout_seconds: int = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        warn_at_seconds: int = DEFAULT_REFRESH_EXPIRING_WARN_SECONDS,
        session_loader: Callable[[], Session | None] | None = None,
        subprocess_runner: Callable | None = None,
        clock: Callable[[], float] | None = None,
        on_rotation_success: Callable | None = None,
    ) -> None:
        self._logbook = logbook
        self._notifier = notifier
        self._enabled = enabled
        self._threshold = threshold_seconds
        self._anti_thrash = anti_thrash_seconds
        self._poll = poll_seconds
        self._subprocess_timeout = subprocess_timeout_seconds
        self._warn_at = warn_at_seconds
        self._session_loader = session_loader or load_session
        self._subprocess_runner = subprocess_runner or self._default_runner
        self._clock = clock or time.time
        self._on_rotation_success = on_rotation_success
        # Anti-thrash state.
        self._last_attempt_ts: float | None = None
        # Whether we've already warned at the 15-min threshold this
        # rotation cycle.
        self._warned_expiring = False
        self._task: asyncio.Task | None = None

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            self._logbook.info("auth_monitor.disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        self._logbook.info(
            "auth_monitor.started",
            threshold_seconds=self._threshold,
            anti_thrash_seconds=self._anti_thrash,
            poll_seconds=self._poll,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # ---- loop ----------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except Exception as e:
                    # One loop iteration's failure must not kill the
                    # monitor — log and continue.
                    self._logbook.error(
                        "auth_monitor.tick_error",
                        error=f"{type(e).__name__}: {e}",
                    )
                await asyncio.sleep(self._poll)
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        """One check-and-maybe-rotate iteration."""
        session = self._session_loader()
        if session is None:
            return
        now = int(self._clock())
        remaining = session.refresh_token_expires_at - now

        # 15-minute warning (fires once per rotation cycle).
        if (
            0 < remaining <= self._warn_at
            and not self._warned_expiring
            and self._attempt_allowed(now) is False
        ):
            self._logbook.warning(
                "auth.refresh_expiring",
                remaining_seconds=remaining,
            )
            self._notifier.emit(
                "auth.refresh_expiring",
                remaining_seconds=remaining,
            )
            self._warned_expiring = True

        # Rotation trigger.
        if remaining <= self._threshold and self._attempt_allowed(now):
            await self.run_once(reason="scheduled")

    # ---- rotation primitive -------------------------------------------

    async def run_once(self, *, reason: str) -> AuthMonitorResult:
        """Attempt one rotation. Exposed so the `reauth` MCP tool
        can force a rotation outside the normal schedule."""
        now = self._clock()
        self._last_attempt_ts = now
        self._logbook.info(
            "auth.auto_login.started", reason=reason,
        )
        env = {**os.environ, "HEADLESS": "1"}
        # Shell out to our own `auth --force` — single source of
        # truth for the browser flow lives in commands/auth.py.
        # Console-script renamed schwab_cli → schwab in PR #6.
        import shutil
        binary = (
            shutil.which("schwab")
            or shutil.which("schwab_cli")
            or "schwab"
        )
        started = self._clock()
        try:
            returncode, stderr = await self._subprocess_runner(
                [binary, "auth", "--force"],
                env=env,
                timeout=self._subprocess_timeout,
            )
        except asyncio.TimeoutError:
            duration = self._clock() - started
            result = AuthMonitorResult(
                ok=False,
                stderr_tail="timed out",
                duration_sec=duration,
            )
            self._emit_failed(result, reason=reason)
            return result
        except Exception as e:
            duration = self._clock() - started
            result = AuthMonitorResult(
                ok=False,
                stderr_tail=f"{type(e).__name__}: {e}",
                duration_sec=duration,
            )
            self._emit_failed(result, reason=reason)
            return result

        duration = self._clock() - started
        ok = returncode == 0
        tail = _tail(stderr)
        result = AuthMonitorResult(ok=ok, stderr_tail=tail, duration_sec=duration)
        if ok:
            self._warned_expiring = False  # reset for next cycle
            self._logbook.info(
                "auth.auto_login.succeeded",
                duration_sec=round(duration, 2),
                reason=reason,
            )
            self._notifier.emit(
                "auth.auto_login.succeeded",
                duration_sec=round(duration, 2),
                reason=reason,
            )
            if self._on_rotation_success is not None:
                try:
                    await self._on_rotation_success()
                except Exception as e:
                    self._logbook.warning(
                        "auth.on_rotation_hook_error",
                        error=f"{type(e).__name__}: {e}",
                    )
        else:
            self._emit_failed(result, reason=reason)
        return result

    def _emit_failed(self, result: AuthMonitorResult, *, reason: str) -> None:
        self._logbook.error(
            "auth.auto_login.failed",
            reason=reason,
            duration_sec=round(result.duration_sec, 2),
            stderr_tail=result.stderr_tail,
        )
        self._notifier.emit(
            "auth.auto_login.failed",
            reason=reason,
            stderr_tail=result.stderr_tail,
        )

    # ---- anti-thrash ---------------------------------------------------

    def _attempt_allowed(self, now: float) -> bool:
        """True when we're past the anti-thrash window since the
        last attempt (success or failure)."""
        if self._last_attempt_ts is None:
            return True
        return (now - self._last_attempt_ts) >= self._anti_thrash

    # ---- subprocess default runner ------------------------------------

    async def _default_runner(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        return proc.returncode or 0, stderr


def _tail(s: str, max_lines: int = 3, max_chars: int = 400) -> str:
    """Last few lines of a subprocess stderr, capped for
    notification bodies."""
    if not s:
        return ""
    lines = s.strip().splitlines()[-max_lines:]
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        return joined[-max_chars:]
    return joined
