"""Unit tests for the shared CLI error mapper (`commands/_error.py`).

Pins the canonical message + exit-code mapping that every thin command now
routes through, plus the pass-through / mapping behavior of the `cli_errors`
decorator.
"""
from __future__ import annotations

import pytest
import typer

from schwab_cli.commands._error import cli_errors, handle_cli_error
from schwab_cli.service import ServiceError
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)


def _exit_of(exc: Exception) -> typer.Exit:
    with pytest.raises(typer.Exit) as ei:
        handle_cli_error(exc)
    return ei.value


class TestHandleCliError:
    def test_not_configured_message(self, capsys):
        exit_ = _exit_of(NotConfigured())
        assert exit_.exit_code == 1
        assert "No config" in capsys.readouterr().err

    def test_not_authenticated_message(self, capsys):
        exit_ = _exit_of(NotAuthenticated())
        assert exit_.exit_code == 1
        assert "No session" in capsys.readouterr().err

    def test_service_error_uses_str(self, capsys):
        exit_ = _exit_of(ServiceError("no skew data for FOO"))
        assert exit_.exit_code == 1
        assert "no skew data for FOO" in capsys.readouterr().err

    def test_api_error_uses_message(self, capsys):
        exit_ = _exit_of(ApiError("503 down"))
        assert exit_.exit_code == 1
        assert "503 down" in capsys.readouterr().err

    def test_session_expired_uses_message(self, capsys):
        exit_ = _exit_of(SessionExpired("token expired"))
        assert exit_.exit_code == 1
        assert "token expired" in capsys.readouterr().err

    def test_empty_message_falls_back_to_class_name(self, capsys):
        exit_ = _exit_of(ApiError())
        assert exit_.exit_code == 1
        assert "ApiError" in capsys.readouterr().err

    def test_always_exit_code_1(self):
        for exc in (
            NotConfigured(),
            NotAuthenticated(),
            ServiceError("x"),
            ApiError("y"),
            SessionExpired("z"),
        ):
            assert _exit_of(exc).exit_code == 1


class TestCliErrorsDecorator:
    def test_passthrough_return_value(self):
        @cli_errors
        def ok(a, b):
            return a + b

        assert ok(2, 3) == 5

    def test_passthrough_preserves_kwargs(self):
        @cli_errors
        def ok(*, x):
            return x

        assert ok(x="hi") == "hi"

    def test_maps_not_configured(self, capsys):
        @cli_errors
        def boom():
            raise NotConfigured()

        with pytest.raises(typer.Exit) as ei:
            boom()
        assert ei.value.exit_code == 1
        assert "No config" in capsys.readouterr().err

    def test_maps_service_error(self, capsys):
        @cli_errors
        def boom():
            raise ServiceError("contract not found")

        with pytest.raises(typer.Exit) as ei:
            boom()
        assert ei.value.exit_code == 1
        assert "contract not found" in capsys.readouterr().err

    def test_maps_api_error(self, capsys):
        @cli_errors
        def boom():
            raise ApiError("503 down")

        with pytest.raises(typer.Exit) as ei:
            boom()
        assert ei.value.exit_code == 1
        assert "503 down" in capsys.readouterr().err

    def test_does_not_catch_unrelated_exceptions(self):
        @cli_errors
        def boom():
            raise ValueError("not a service error")

        with pytest.raises(ValueError):
            boom()

    def test_parse_time_exit_passes_through(self):
        # A typer.Exit(2) raised inside the command for parse-time validation
        # must NOT be swallowed/remapped by the decorator.
        @cli_errors
        def boom():
            raise typer.Exit(code=2)

        with pytest.raises(typer.Exit) as ei:
            boom()
        assert ei.value.exit_code == 2

    def test_wraps_preserves_name(self):
        @cli_errors
        def named_fn():
            return None

        assert named_fn.__name__ == "named_fn"
