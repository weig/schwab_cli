"""Shared CLI error mapping for Layer-3 commands.

Every thin command routes the common auth / API / service failures through
:func:`handle_cli_error` (directly, or via the :func:`cli_errors` decorator)
so the canonical stderr message + exit-code mapping lives in one place rather
than being copy-pasted into each command's ``except`` ladder.

Parse-time validation errors (``FormatError``, ``TickerError``, interval /
range spec errors, ``OptionSpecError``) are deliberately *not* handled here —
they exit with code 2 and stay in the command. This module only owns the
service / auth / API error -> stderr + exit 1 mapping.
"""
from __future__ import annotations

import functools
import typing

import typer


def handle_cli_error(e: Exception) -> typing.NoReturn:
    """Map a service / auth exception to the canonical stderr message + exit 1.

    The single place all commands route auth / API / service errors:

    * :class:`NotConfigured`   -> ``"No config found. Run `schwab_cli setup` first."``
    * :class:`NotAuthenticated` -> ``"No session found. Run `schwab_cli auth` first."``
    * any other ``ServiceError`` (carrying a complete message) / ``ApiError`` /
      ``SessionExpired`` -> ``str(e)`` (falling back to the class name when the
      exception has no message).
    """
    from schwab_cli.service.auth import NotAuthenticated, NotConfigured

    if isinstance(e, NotConfigured):
        msg = "No config found. Run `schwab_cli setup` first."
    elif isinstance(e, NotAuthenticated):
        msg = "No session found. Run `schwab_cli auth` first."
    else:  # ServiceError (with message) / ApiError / SessionExpired
        msg = str(e) or type(e).__name__
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def cli_errors(fn: typing.Callable) -> typing.Callable:
    """Decorator: run ``fn``, routing the common service / auth / API errors
    through :func:`handle_cli_error`.

    Parse-time errors (``FormatError``, ``TickerError``, range ``.kind``,
    ``OptionSpecError``) are NOT caught here — they stay in the command and
    keep their own exit codes (typically 2).
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        from schwab_cli.service import ServiceError
        from schwab_cli.service.auth import ApiError, SessionExpired

        try:
            return fn(*args, **kwargs)
        except (ServiceError, ApiError, SessionExpired) as e:
            handle_cli_error(e)

    return _wrapper
