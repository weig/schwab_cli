"""`vol` command (thin) — IV / HV / HVP / P/C Ratio / IVP.

Layer-3 shim: parse + validate flags/args (exit 2), call
``service.vol.get_vol``, map service errors to exit codes, and render.
All orchestration, auth, and the synthetic-IV backfill live in
:mod:`schwab_cli.service.vol`.

``compute_iv_rank_and_percentile`` moved to ``service.vol``; external
callers import it from there now.
"""

from __future__ import annotations

import typer

from schwab_cli.commands._error import cli_errors
from schwab_cli.commands._output import null_sink, vol_cli_sink
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.vol import render_vol
from schwab_cli.service.vol import VolService, VolStorageError
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


@cli_errors
def run(
    symbol: str,
    *,
    hv_window: int = 30,
    hv_lookback: int = 252,
    ivp_lookback: int = 252,
    no_record: bool = False,
    snapshot_only: bool = False,
    as_json: bool = False,
    as_md: bool = False,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        ticker = resolve_ticker(symbol)
    except TickerError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if ticker.type != "stock":
        typer.secho(
            f"vol expects a stock ticker, got {ticker.type}: {symbol!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # Per-day backfill progress lines stream to stdout in human mode only —
    # keeps JSON / MD / snapshot-only output clean for piping. The one-line
    # backfill notice goes to stderr, also human-mode only. The CliSink
    # reproduces the exact CYAN text + stream of the old callbacks; the
    # NullSink swallows everything in machine-facing modes.
    human = not (as_json or as_md or snapshot_only)
    out = vol_cli_sink() if human else null_sink()

    # VolStorageError is the one service error that prints in YELLOW (not the
    # canonical RED), so it stays local; NoVolData / auth / API errors carry the
    # canonical RED + exit-1 mapping and are routed through @cli_errors.
    try:
        result = VolService(out=out).get_vol(
            ticker.underlying,
            hv_window=hv_window,
            hv_lookback=hv_lookback,
            ivp_lookback=ivp_lookback,
            no_record=no_record,
            snapshot_only=snapshot_only,
        )
    except VolStorageError as e:
        typer.secho(str(e), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    if result is None:
        # --snapshot-only: accumulated silently, nothing to render.
        return

    if result.storage_error:
        typer.secho(
            f"vol storage warning (IVP may be stale): {result.storage_error}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    typer.echo(render_vol(result.envelope, fmt=fmt))
