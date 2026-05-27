"""CLI output sinks for Layer-2 services (PR #52 review #11).

The service layer emits user-facing progress / notice text through an
injected :class:`~schwab_cli.service.base.OutputSink`. In human-facing CLI
modes we wire a :class:`CliSink` that reproduces the EXACT stderr/stdout
text + ``typer.secho`` colors the old ``vol`` / ``skew`` callbacks emitted;
in machine-facing modes (``--json`` / ``--md`` / ``--snapshot-only``) and
from MCP / REST we use a no-op sink so nothing pollutes the payload.

Color / stream are matched precisely to the pre-refactor commands:

* ``vol`` progress lines -> CYAN, **stdout** (``typer.secho(..., fg=CYAN)``).
* ``vol`` backfill notice -> CYAN, **stderr** (``..., err=True``).
* ``skew`` partial-failure skip -> YELLOW, **stderr** (``..., err=True``).
"""

from __future__ import annotations

import typer

from schwab_cli.service.base import NullSink


class CliSink:
    """Output sink that prints via ``typer.secho`` with configurable
    color + stream per channel, matching the pre-refactor command output.

    ``info`` and ``progress`` each carry their own ``(color, err)`` pair so a
    single class can reproduce both the vol (CYAN) and skew (YELLOW) text.
    """

    def __init__(
        self,
        *,
        info_color: str,
        info_err: bool,
        progress_color: str,
        progress_err: bool,
    ) -> None:
        self._info_color = info_color
        self._info_err = info_err
        self._progress_color = progress_color
        self._progress_err = progress_err

    def info(self, message: str) -> None:
        typer.secho(message, fg=self._info_color, err=self._info_err)

    def progress(self, message: str) -> None:
        typer.secho(message, fg=self._progress_color, err=self._progress_err)


def vol_cli_sink() -> CliSink:
    """Sink for the human-mode ``vol`` command.

    Progress lines stream to **stdout** in CYAN; the one-line backfill
    notice goes to **stderr** in CYAN — byte-identical to the old
    ``progress`` / ``on_backfill_notice`` callbacks.
    """
    return CliSink(
        info_color=typer.colors.CYAN,
        info_err=True,
        progress_color=typer.colors.CYAN,
        progress_err=False,
    )


def skew_cli_sink() -> CliSink:
    """Sink for the ``skew`` command.

    Partial-failure skip notices go to **stderr** in YELLOW — byte-identical
    to the old ``_warn`` / ``on_skip`` callback. ``skew`` never streams
    per-step progress, so ``progress`` mirrors the same YELLOW/stderr config
    (it is never invoked by the service).
    """
    return CliSink(
        info_color=typer.colors.YELLOW,
        info_err=True,
        progress_color=typer.colors.YELLOW,
        progress_err=True,
    )


def null_sink() -> NullSink:
    """No-op sink for machine-facing modes (``--json`` / ``--md`` /
    ``--snapshot-only``) and MCP / REST."""
    return NullSink()
