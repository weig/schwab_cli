"""TDD red-phase tests for Phase 3 server-jobs wiring in commands/server.py.

Two test classes:
1. TestSignalHandlerInstallation — fast unit test (no subprocess) asserting
   that _install_signal_handlers (or equivalent) registers SIGHUP in addition
   to SIGTERM/SIGINT.
2. TestServerJobsSubprocessIntegration — real-subprocess test that exercises
   the full SIGHUP→reload and SIGTERM→clean-shutdown lifecycle.

The subprocess test is bounded by generous polling + a hard kill in finally so
it cannot hang CI. All tests will FAIL until the runtime module and the server
wiring are implemented.

Run with:
    uv run --frozen --extra dev python -m pytest tests/test_commands_server_jobs.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guards — always collect cleanly, even before implementation exists
# ---------------------------------------------------------------------------

try:
    from schwab_cli.commands import server as server_cmd
    _SERVER_CMD_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    server_cmd = None  # type: ignore[assignment]
    _SERVER_CMD_AVAILABLE = False

try:
    from schwab_cli.server.jobs import runtime as runtime_mod
    _RUNTIME_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    runtime_mod = None  # type: ignore[assignment]
    _RUNTIME_AVAILABLE = False

try:
    from schwab_cli.server.jobs.state import load_state
    _STATE_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _STATE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = 0.25
_DAEMON_STARTUP_TIMEOUT_S = 15
_SIGNAL_SETTLE_TIMEOUT_S = 12
_SHUTDOWN_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_minimal_config(config_dir: Path) -> None:
    """Write the minimum valid config.json into config_dir."""
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    }
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_minimal_session(config_dir: Path) -> None:
    """Write a session.json with a far-future refresh token expiry."""
    now = int(time.time())
    session = {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "expires_at": now + 3600,
        "refresh_token_expires_at": now + 7 * 24 * 3600,
    }
    (config_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")


def _write_job_file(jobs_dir: Path, job_id: str, *, enabled: bool) -> Path:
    """Write a minimal job JSON file."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": f"Test Job {job_id}",
        "enabled": enabled,
        "cron": "0 9 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["schwab", "quote", "AAPL"],
    }
    p = jobs_dir / f"{job_id}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _poll_until(condition, timeout_s: float, interval_s: float = _POLL_INTERVAL_S) -> bool:
    """Poll condition() up to timeout_s seconds. Returns True if condition was met."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:  # noqa: BLE001 — keep polling on transient errors
            pass
        time.sleep(interval_s)
    return False


def _resolve_schwab_binary() -> str:
    """Return the path to the schwab console script for the current environment."""
    # Try shutil.which first (works when installed as a console script).
    which = shutil.which("schwab")
    if which:
        return which
    # Fall back to running as a module — always works in the project's .venv.
    return None  # sentinel: caller uses [sys.executable, "-m", "schwab_cli"]


def _schwab_argv() -> list[str]:
    """Return the argv prefix to invoke the schwab CLI in this environment."""
    binary = _resolve_schwab_binary()
    if binary:
        return [binary]
    return [sys.executable, "-m", "schwab_cli"]


# ---------------------------------------------------------------------------
# Unit test: signal handler registration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SERVER_CMD_AVAILABLE,
    reason="schwab_cli.commands.server not available yet",
)
class TestSignalHandlerInstallation:
    """_install_signal_handlers must register SIGHUP in addition to SIGTERM/SIGINT."""

    def test_sighup_is_registered(self, monkeypatch):
        """After calling _install_signal_handlers, SIGHUP must be among the signals set."""
        installed: list[int] = []
        original_signal = signal.signal

        def recording_signal(signum, handler):
            installed.append(signum)
            # Don't actually install — keep the test runner's own handlers intact.

        monkeypatch.setattr(signal, "signal", recording_signal)
        handler = MagicMock()
        server_cmd._install_signal_handlers(handler)

        assert signal.SIGHUP in installed, (
            f"SIGHUP not installed. Installed signals: {installed}. "
            "_install_signal_handlers must be extended to also register SIGHUP "
            "so the daemon can reload its job config without restarting."
        )

    def test_sigterm_is_still_registered(self, monkeypatch):
        """SIGTERM must remain registered (pre-existing behaviour)."""
        installed: list[int] = []
        monkeypatch.setattr(signal, "signal", lambda sig, h: installed.append(sig))
        server_cmd._install_signal_handlers(MagicMock())
        assert signal.SIGTERM in installed

    def test_sigint_is_still_registered(self, monkeypatch):
        """SIGINT must remain registered (pre-existing behaviour)."""
        installed: list[int] = []
        monkeypatch.setattr(signal, "signal", lambda sig, h: installed.append(sig))
        server_cmd._install_signal_handlers(MagicMock())
        assert signal.SIGINT in installed

    def test_install_is_noop_off_main_thread(self, monkeypatch):
        """Off the main thread signal.signal raises ValueError; handler must swallow it."""
        def raise_value_error(sig, handler):
            raise ValueError("signal only works in main thread")

        monkeypatch.setattr(signal, "signal", raise_value_error)
        # Must not raise
        server_cmd._install_signal_handlers(MagicMock())


# ---------------------------------------------------------------------------
# Integration test: real subprocess lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _RUNTIME_AVAILABLE or not _SERVER_CMD_AVAILABLE or not _STATE_AVAILABLE,
    reason="runtime module and server wiring not yet implemented",
)
class TestServerJobsSubprocessIntegration:
    """Real-subprocess lifecycle test for SIGHUP reload + SIGTERM shutdown.

    The daemon is launched with SCHWAB_CLI_CONFIG_DIR pointing at a fresh
    tmp directory. It has one disabled job (so no workers are ever spawned,
    no auth/network needed). The test:
    1. Waits for jobs/.current/server.pid to appear.
    2. Asserts the pidfile contains the daemon's PID.
    3. Asserts the disabled job is in state.json.
    4. Adds a second job, sends SIGHUP, polls for it to appear in state.json.
    5. Sends SIGTERM and asserts clean exit (code 0) + pidfile removed.

    A hard-kill in the finally block ensures the test never hangs CI.
    """

    @pytest.fixture()
    def daemon_env(self, tmp_path):
        """Set up a self-contained config dir and return relevant paths."""
        config_dir = tmp_path / "config"
        _write_minimal_config(config_dir)
        _write_minimal_session(config_dir)

        jobs_dir = config_dir / "jobs"
        _write_job_file(jobs_dir, "alpha", enabled=False)

        env = os.environ.copy()
        env["SCHWAB_CLI_CONFIG_DIR"] = str(config_dir)
        # Notifications are isolated by SCHWAB_CLI_CONFIG_DIR: notify_config
        # resolves notification.json under the (tmp) config dir, so the spawned
        # daemon finds no config there and the notifier is inert — it can NEVER
        # send a real alert during the test. (HOME is also redirected as a
        # belt-and-suspenders guard against any hardcoded ~/.config lookups.)
        env["HOME"] = str(config_dir)

        current_dir = jobs_dir / ".current"
        return {
            "config_dir": config_dir,
            "jobs_dir": jobs_dir,
            "current_dir": current_dir,
            "env": env,
        }

    def _launch_daemon(self, daemon_env: dict) -> subprocess.Popen:
        """Launch `schwab server` with a very short interval and return the Popen handle."""
        argv = _schwab_argv() + [
            "server",
            "--interval-hours", "0.00028",  # ~1 second — fastest settable interval
            "--no-auto-login",
            "--no-log-file",
        ]
        return subprocess.Popen(
            argv,
            env=daemon_env["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _read_output(self, proc: subprocess.Popen) -> str:
        """Drain stdout (non-blocking, best-effort) for diagnostics."""
        try:
            out, _ = proc.communicate(timeout=0.1)
            return out.decode(errors="replace") if out else ""
        except subprocess.TimeoutExpired:
            return ""

    def test_pidfile_written_on_startup(self, daemon_env):
        """Daemon must write server.pid with its own PID within startup timeout."""
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"

        proc = self._launch_daemon(daemon_env)
        try:
            started = _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)
            assert started, (
                f"server.pid not created within {_DAEMON_STARTUP_TIMEOUT_S}s. "
                f"current_dir={current_dir}. "
                f"Process alive: {proc.poll() is None}."
            )

            data = runtime_mod.read_pidfile(current_dir)
            assert data is not None, "server.pid exists but read_pidfile returned None"
            assert data["pid"] == proc.pid, (
                f"pidfile pid={data['pid']} != subprocess pid={proc.pid}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_disabled_job_promoted_to_current(self, daemon_env):
        """The disabled job must be promoted to jobs/.current/alpha.json on startup."""
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"
        job_file = current_dir / "alpha.json"

        proc = self._launch_daemon(daemon_env)
        try:
            # Wait for pidfile as proxy for "startup complete".
            _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)

            appeared = _poll_until(job_file.exists, _DAEMON_STARTUP_TIMEOUT_S)
            assert appeared, (
                f"alpha.json not promoted to current within {_DAEMON_STARTUP_TIMEOUT_S}s. "
                f"current_dir contents: {list(current_dir.iterdir()) if current_dir.exists() else 'dir missing'}."
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_disabled_job_appears_in_state_json(self, daemon_env):
        """After startup, state.json must contain the 'alpha' job entry."""
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"

        proc = self._launch_daemon(daemon_env)
        try:
            _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)

            def alpha_in_state():
                state = load_state(current_dir)
                return "alpha" in state.jobs

            found = _poll_until(alpha_in_state, _DAEMON_STARTUP_TIMEOUT_S)
            assert found, (
                "Job 'alpha' not found in state.json after startup. "
                f"State: {load_state(current_dir)}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_sighup_reloads_new_job_without_killing_process(self, daemon_env):
        """SIGHUP must trigger a reload: new job appears in state.json, process stays alive."""
        config_dir = daemon_env["config_dir"]
        jobs_dir = daemon_env["jobs_dir"]
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"

        proc = self._launch_daemon(daemon_env)
        try:
            # Wait for daemon to be fully up.
            started = _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)
            assert started, "Daemon did not start (no pidfile)"

            # Drop a second job into the staging dir.
            _write_job_file(jobs_dir, "beta", enabled=False)

            # Send SIGHUP to trigger reload.
            proc.send_signal(signal.SIGHUP)

            # Poll for beta to appear in state.json (reload must have run).
            def beta_in_state():
                state = load_state(current_dir)
                return "beta" in state.jobs

            reloaded = _poll_until(beta_in_state, _SIGNAL_SETTLE_TIMEOUT_S)

            # Process must still be alive after SIGHUP.
            assert proc.poll() is None, (
                "Process died after SIGHUP (expected SIGHUP to reload, not terminate). "
                f"Exit code: {proc.poll()}"
            )
            assert reloaded, (
                f"Job 'beta' did not appear in state.json within {_SIGNAL_SETTLE_TIMEOUT_S}s "
                "after SIGHUP. Reload may not be wired up."
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_sigterm_causes_clean_exit_and_removes_pidfile(self, daemon_env):
        """SIGTERM must cause exit code 0 and remove server.pid."""
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"

        proc = self._launch_daemon(daemon_env)
        try:
            started = _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)
            assert started, "Daemon did not start (no pidfile) — cannot test shutdown"

            proc.send_signal(signal.SIGTERM)

            def process_exited():
                return proc.poll() is not None

            exited = _poll_until(process_exited, _SHUTDOWN_TIMEOUT_S)
            assert exited, (
                f"Process did not exit within {_SHUTDOWN_TIMEOUT_S}s after SIGTERM."
            )

            exit_code = proc.poll()
            assert exit_code == 0, (
                f"Expected clean exit (0) after SIGTERM; got {exit_code}."
            )

            assert not pidfile.exists(), (
                "server.pid was not removed after clean shutdown. "
                "The daemon must call remove_pidfile() in its cleanup path."
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_process_alive_after_sighup_explicitly(self, daemon_env):
        """An isolated check: process is alive immediately after SIGHUP."""
        current_dir = daemon_env["current_dir"]
        pidfile = current_dir / "server.pid"

        proc = self._launch_daemon(daemon_env)
        try:
            started = _poll_until(pidfile.exists, _DAEMON_STARTUP_TIMEOUT_S)
            assert started, "Daemon did not start"

            proc.send_signal(signal.SIGHUP)
            # Give it a moment to process the signal — should NOT exit.
            time.sleep(1.0)

            assert proc.poll() is None, (
                f"Process exited after SIGHUP with code {proc.poll()}. "
                "SIGHUP must trigger reload, not shutdown."
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.skipif(
    not _SERVER_CMD_AVAILABLE,
    reason="server command wiring not yet implemented",
)
class TestStartJobsRaisesInFinally:
    """Regression: if _start_jobs raises, the finally must not mask it.

    Before the fix, ``scheduler`` / ``jobs_thread`` were only assigned by the
    return of ``_start_jobs``; when it raised they were never bound and the
    ``finally`` calling ``_stop_jobs(scheduler, jobs_thread, ...)`` died with
    ``UnboundLocalError``, masking the original exception. They are now
    initialised to ``None`` before the try, so the original error propagates.
    """

    @pytest.fixture
    def isolated_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        _write_minimal_config(config_dir)
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(config_dir))
        return config_dir

    @staticmethod
    def _stub_token_runtime(monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.build_token_manager",
            lambda cfg, **kw: MagicMock(name="token_manager"),
        )

        def _fake_start(mgr, stop):
            stop.set()  # never park: release the stop event immediately
            return ()

        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.start_token_threads", _fake_start,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.stop_token_threads",
            lambda *a, **k: None,
        )

    def test_default_run_propagates_start_jobs_error(
        self, isolated_config, monkeypatch
    ):
        sentinel = RuntimeError("boom from _start_jobs")

        def _boom(*_a, **_k):
            raise sentinel

        monkeypatch.setattr(server_cmd, "_start_jobs", _boom)
        # Keep the token runtime hermetic: no real threads, and the stop
        # event released so a regression that skips the raise can't hang.
        self._stub_token_runtime(monkeypatch)

        with pytest.raises(RuntimeError) as exc:
            server_cmd.run()
        # The ORIGINAL error propagates — NOT an UnboundLocalError from the
        # finally trying to use an unbound scheduler/jobs_thread.
        assert exc.value is sentinel
        assert not isinstance(exc.value, UnboundLocalError)

    def test_finally_cleanup_does_not_crash(self, isolated_config, monkeypatch):
        """The finally's _stop_jobs/remove_pidfile run cleanly with None args."""
        monkeypatch.setattr(
            server_cmd, "_start_jobs",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("nope")),
        )
        self._stub_token_runtime(monkeypatch)
        # Must raise the ValueError, and the finally (None-safe _stop_jobs +
        # remove_pidfile) must not itself raise.
        with pytest.raises(ValueError, match="nope"):
            server_cmd.run()


# ---------------------------------------------------------------------------
# Bug 2 (integration) — _start_jobs must seed the scheduler from persisted state
# ---------------------------------------------------------------------------
#
# Seam: _start_jobs passes ``initial_states`` (loaded from state.json via
# load_state()) to JobScheduler.__init__ so that a job's last_run_at /
# last_status from a previous daemon instance survive a restart.
#
# This test patches enough of the environment so _start_jobs can run without a
# real daemon / real workers: spawn is mocked to never actually fork, and the
# scheduler loop thread is never started (we call _start_jobs but immediately
# inspect the returned scheduler before any tick runs).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SERVER_CMD_AVAILABLE or not _STATE_AVAILABLE or not _RUNTIME_AVAILABLE,
    reason="server command, state, and runtime modules not yet available",
)
class TestStartJobsSeedsFromPersistedState:
    """_start_jobs must pass persisted run-history into the JobScheduler constructor.

    Seam assumed by the implementation:
      JobScheduler.__init__ gains  ``initial_states: Mapping[str, JobRunState] | None = None``
      _start_jobs calls  ``load_state(curr)`` and passes ``state.jobs`` as
      ``initial_states`` when constructing the scheduler — so that a job's
      ``last_run_at`` / ``last_status`` are preserved across a restart even
      though jobs=[] is still passed (config is loaded via reload(), not __init__).
    """

    @pytest.fixture
    def isolated_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        _write_minimal_config(config_dir)
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(config_dir))
        return config_dir

    def test_start_jobs_preserves_last_run_at_from_state_json(
        self, isolated_config, monkeypatch, tmp_path
    ):
        """A job whose last_run_at is in state.json must still report it after _start_jobs.

        Setup:
          1. Write a valid job config (accounts) to the staging jobs dir.
          2. Write a state.json under .current with last_run_at=999.0 for "accounts".
          3. Call _start_jobs (with mocked spawn so no real workers fork).
          4. Call scheduler.snapshot() and assert last_run_at==999.0.

        This fails today because _start_jobs builds JobScheduler(jobs=[])
        with an empty _states dict, discarding the persisted history.
        """
        from schwab_cli.server.jobs.state import (
            JobRunState,
            SchedulerState,
            save_state,
        )
        import threading

        config_dir = isolated_config
        jobs_dir = config_dir / "jobs"
        current_dir = jobs_dir / ".current"

        # Write a valid job config to staging.
        _write_job_file(jobs_dir, "accounts", enabled=False)

        # Write persisted state with a known last_run_at.
        current_dir.mkdir(parents=True, exist_ok=True)
        save_state(
            current_dir,
            SchedulerState(
                jobs={
                    "accounts": JobRunState(
                        id="accounts",
                        last_run_at=999.0,
                        last_status="ok",
                        last_exit_code=0,
                    )
                },
                updated_at=999.0,
            ),
        )

        # Prevent _start_jobs from actually writing a pidfile or starting a thread.
        monkeypatch.setattr(runtime_mod, "write_pidfile", lambda _: None)

        # Capture the JobScheduler constructor args to verify initial_states was passed.
        captured_schedulers: list = []
        original_scheduler_cls = server_cmd.JobScheduler

        class CapturingScheduler(original_scheduler_cls):
            def __init__(self, **kwargs):
                captured_schedulers.append(kwargs)
                super().__init__(**kwargs)

            def tick(self):
                pass  # no-op — we don't want any real ticks

        monkeypatch.setattr(server_cmd, "JobScheduler", CapturingScheduler)

        # Prevent the loop thread from doing anything real.
        monkeypatch.setattr(
            runtime_mod,
            "run_scheduler_loop",
            lambda *a, **k: None,
        )

        stop_event = threading.Event()
        reload_event = threading.Event()
        wake = threading.Event()

        scheduler, thread, curr = server_cmd._start_jobs(
            cfg=None,  # not used in current implementation
            stop_event=stop_event,
            reload_event=reload_event,
            wake=wake,
            renew=None,
            notify=None,
        )

        # The scheduler's snapshot must carry the persisted last_run_at.
        snap = scheduler.snapshot()
        accounts = snap.jobs.get("accounts")
        assert accounts is not None, (
            "Job 'accounts' must be present in scheduler snapshot after _start_jobs. "
            "It was promoted via apply_reload but its run-state was not seeded."
        )
        assert accounts.last_run_at == pytest.approx(999.0), (
            f"last_run_at must be 999.0 from persisted state.json; "
            f"got {accounts.last_run_at!r}. "
            "_start_jobs must pass initial_states=state.jobs to JobScheduler.__init__."
        )
        assert accounts.last_status == "ok", (
            f"last_status must be 'ok' from persisted state.json; "
            f"got {accounts.last_status!r}"
        )

        # Cleanup: stop the thread.
        stop_event.set()
        wake.set()
        if thread is not None:
            thread.join(timeout=2)
