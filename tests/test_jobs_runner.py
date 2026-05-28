"""TDD red-phase tests for schwab_cli.server.jobs.runner.

These tests will FAIL at collection with ModuleNotFoundError until the
module is implemented — that is the expected RED state.
"""
from __future__ import annotations

import pytest

from schwab_cli.server.jobs import runner as runner_mod
from schwab_cli.server.jobs.config import JobConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _command_cfg(**overrides):
    """Return a minimal command-type JobConfig."""
    base = dict(
        id="test-cmd",
        name="Test Command",
        enabled=True,
        cron="0 9 * * *",
        timezone="UTC",
        type="command",
        command=("schwab", "quote", "NVDA"),
    )
    base.update(overrides)
    return JobConfig(**base)


def _python_cfg(**overrides):
    """Return a minimal python-type JobConfig."""
    base = dict(
        id="test-py",
        name="Test Python",
        enabled=True,
        cron="0 9 * * *",
        timezone="UTC",
        type="python",
        runner="os.getpid",
        args=(),
        kwargs={},
    )
    base.update(overrides)
    return JobConfig(**base)


# ---------------------------------------------------------------------------
# resolve_binary
# ---------------------------------------------------------------------------


class TestResolveBinary:
    """resolve_binary() returns a non-empty path string."""

    def test_returns_non_empty_string(self):
        result = runner_mod.resolve_binary()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_str_not_bytes(self):
        result = runner_mod.resolve_binary()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# command_argv
# ---------------------------------------------------------------------------


class TestCommandArgv:
    """command_argv builds the argv list for a command-type job."""

    def test_uses_provided_binary(self):
        cfg = _command_cfg(command=("quote", "NVDA"))
        argv = runner_mod.command_argv(cfg, binary="/usr/local/bin/schwab")
        assert argv[0] == "/usr/local/bin/schwab"

    def test_appends_command_after_binary(self):
        cfg = _command_cfg(command=("quote", "NVDA"))
        argv = runner_mod.command_argv(cfg, binary="schwab")
        assert argv == ["schwab", "quote", "NVDA"]

    def test_uses_resolved_binary_when_none(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "resolve_binary", lambda: "schwab-resolved")
        cfg = _command_cfg(command=("quote", "AAPL"))
        argv = runner_mod.command_argv(cfg)
        assert argv[0] == "schwab-resolved"

    def test_returns_list(self):
        cfg = _command_cfg(command=("quote", "NVDA"))
        argv = runner_mod.command_argv(cfg, binary="schwab")
        assert isinstance(argv, list)

    def test_raises_for_python_type(self):
        cfg = _python_cfg()
        with pytest.raises(ValueError):
            runner_mod.command_argv(cfg, binary="schwab")

    def test_raises_for_empty_command(self):
        import dataclasses
        cfg = dataclasses.replace(
            _command_cfg(command=("echo",)),
            command=(),
        )
        with pytest.raises(ValueError):
            runner_mod.command_argv(cfg, binary="schwab")

    def test_single_element_command(self):
        cfg = _command_cfg(command=("auth",))
        argv = runner_mod.command_argv(cfg, binary="schwab")
        assert argv == ["schwab", "auth"]


# ---------------------------------------------------------------------------
# import_runner
# ---------------------------------------------------------------------------


class TestImportRunner:
    """import_runner imports a dotted path and returns the callable."""

    # NOTE: ``os`` is now in the runner-module blocklist, so the original
    # os.getpid / os.path.join / os.sep targets are rejected before import.
    # These tests were retargeted to non-blocked modules (json, decimal) that
    # assert the SAME behaviours (valid callable resolves; missing attr raises;
    # non-callable raises). See TestImportRunnerBlocklist for the block checks.

    def test_import_json_dumps(self):
        import json
        result = runner_mod.import_runner("json.dumps")
        assert result is json.dumps

    def test_import_json_decoder(self):
        import json
        result = runner_mod.import_runner("json.JSONDecoder")
        assert result is json.JSONDecoder

    def test_bad_module_raises(self):
        with pytest.raises((ValueError, ModuleNotFoundError, ImportError)):
            runner_mod.import_runner("nope_nope_nope.x")

    def test_missing_attribute_raises(self):
        with pytest.raises(ValueError):
            runner_mod.import_runner("json.totally_missing_attr_xyz")

    def test_non_callable_raises(self):
        # json.__name__ is a string, not callable.
        with pytest.raises(ValueError):
            runner_mod.import_runner("json.__name__")

    def test_returns_callable(self):
        result = runner_mod.import_runner("json.dumps")
        assert callable(result)

    def test_empty_string_raises(self):
        with pytest.raises((ValueError, ModuleNotFoundError, ImportError, AttributeError)):
            runner_mod.import_runner("")

    def test_no_dot_raises(self):
        # A plain module name with no dot has no attribute to look up.
        with pytest.raises((ValueError, ModuleNotFoundError, ImportError, AttributeError)):
            runner_mod.import_runner("json")


class TestImportRunnerBlocklist:
    """import_runner rejects dangerous top-level modules before importing."""

    def test_builtins_eval_rejected(self):
        with pytest.raises(ValueError):
            runner_mod.import_runner("builtins.eval")

    def test_subprocess_run_rejected(self):
        with pytest.raises(ValueError):
            runner_mod.import_runner("subprocess.run")

    def test_os_getpid_rejected(self):
        with pytest.raises(ValueError):
            runner_mod.import_runner("os.getpid")

    def test_blocked_submodule_rejected(self):
        # Blocking is by top-level module, so os.path.join is rejected too.
        with pytest.raises(ValueError):
            runner_mod.import_runner("os.path.join")

    def test_permitted_module_resolves(self):
        # A non-blocked importable callable still resolves normally.
        result = runner_mod.import_runner(
            "schwab_cli.server.jobs.runner.resolve_binary"
        )
        assert result is runner_mod.resolve_binary


# ---------------------------------------------------------------------------
# execute_job — command dispatch
# ---------------------------------------------------------------------------


class TestExecuteJobCommand:
    """execute_job dispatches command-type jobs via os.execvp."""

    def test_calls_execvp_with_correct_file(self, monkeypatch):
        execvp_calls = []

        def _fake_execvp(file, argv):
            execvp_calls.append((file, argv))
            raise RuntimeError("sentinel")  # prevent actual exec

        monkeypatch.setattr(runner_mod.os, "execvp", _fake_execvp)
        monkeypatch.setattr(runner_mod, "resolve_binary", lambda: "schwab")

        cfg = _command_cfg(command=("quote", "NVDA"))
        try:
            runner_mod.execute_job(cfg)
        except RuntimeError:
            pass

        assert len(execvp_calls) == 1
        file_arg, _ = execvp_calls[0]
        assert file_arg == "schwab"

    def test_calls_execvp_with_full_argv(self, monkeypatch):
        execvp_calls = []

        def _fake_execvp(file, argv):
            execvp_calls.append((file, argv))
            raise RuntimeError("sentinel")

        monkeypatch.setattr(runner_mod.os, "execvp", _fake_execvp)
        monkeypatch.setattr(runner_mod, "resolve_binary", lambda: "schwab")

        cfg = _command_cfg(command=("quote", "NVDA"))
        try:
            runner_mod.execute_job(cfg)
        except RuntimeError:
            pass

        _, argv_arg = execvp_calls[0]
        assert argv_arg == ["schwab", "quote", "NVDA"]

    def test_returns_127_on_oserror(self, monkeypatch):
        def _fail_execvp(file, argv):
            raise OSError("no such file")

        monkeypatch.setattr(runner_mod.os, "execvp", _fail_execvp)
        monkeypatch.setattr(runner_mod, "resolve_binary", lambda: "schwab")

        cfg = _command_cfg(command=("quote", "NVDA"))
        rc = runner_mod.execute_job(cfg)
        assert rc == 127


# ---------------------------------------------------------------------------
# execute_job — python dispatch
# ---------------------------------------------------------------------------


class TestExecuteJobPython:
    """execute_job dispatches python-type jobs via import_runner."""

    def test_python_success_returns_0(self, monkeypatch):
        called_with = {}

        def _fn(*args, **kwargs):
            called_with["args"] = args
            called_with["kwargs"] = kwargs

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(
            runner="some.module.fn",
            args=("a", "b"),
            kwargs={"x": 1},
        )
        rc = runner_mod.execute_job(cfg)
        assert rc == 0

    def test_python_success_calls_fn_with_args(self, monkeypatch):
        called_with = {}

        def _fn(*args, **kwargs):
            called_with["args"] = args
            called_with["kwargs"] = kwargs

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(
            runner="some.module.fn",
            args=("a", "b"),
            kwargs={"x": 1},
        )
        runner_mod.execute_job(cfg)
        assert called_with["args"] == ("a", "b")
        assert called_with["kwargs"] == {"x": 1}

    def test_python_session_expired_returns_2(self, monkeypatch):
        from schwab_cli.api.client import SessionExpired

        def _fn(*args, **kwargs):
            raise SessionExpired("expired")

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn")
        rc = runner_mod.execute_job(cfg)
        assert rc == 2

    def test_python_not_authenticated_returns_2(self, monkeypatch):
        from schwab_cli.service.auth import NotAuthenticated

        def _fn(*args, **kwargs):
            raise NotAuthenticated("not authed")

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn")
        rc = runner_mod.execute_job(cfg)
        assert rc == 2

    def test_python_generic_exception_returns_1(self, monkeypatch):
        def _fn(*args, **kwargs):
            raise ValueError("something went wrong")

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn")
        rc = runner_mod.execute_job(cfg)
        assert rc == 1

    def test_python_system_exit_3_returns_3(self, monkeypatch):
        def _fn(*args, **kwargs):
            raise SystemExit(3)

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn")
        rc = runner_mod.execute_job(cfg)
        assert rc == 3

    def test_python_system_exit_0_returns_0(self, monkeypatch):
        def _fn(*args, **kwargs):
            raise SystemExit(0)

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn")
        rc = runner_mod.execute_job(cfg)
        assert rc == 0

    def test_exit_auth_failed_constant_is_2(self):
        from schwab_cli._exit_codes import EXIT_AUTH_FAILED
        assert EXIT_AUTH_FAILED == 2

    def test_python_no_args_no_kwargs(self, monkeypatch):
        """A python job with empty args/kwargs still calls the fn."""
        called = []

        def _fn(*args, **kwargs):
            called.append((args, kwargs))

        monkeypatch.setattr(runner_mod, "import_runner", lambda dotted: _fn)

        cfg = _python_cfg(runner="some.module.fn", args=(), kwargs={})
        rc = runner_mod.execute_job(cfg)
        assert rc == 0
        assert called == [((), {})]


# ---------------------------------------------------------------------------
# execute_job — unknown type guard
# ---------------------------------------------------------------------------


class TestExecuteJobUnknownType:
    """execute_job returns 1 for a config carrying an unexpected type."""

    def test_unknown_type_returns_1(self):
        # JobConfig is a plain frozen dataclass with no type validation, so a
        # bogus type can be constructed directly to exercise the guard.
        cfg = _command_cfg(type="bogus")
        rc = runner_mod.execute_job(cfg)
        assert rc == 1
