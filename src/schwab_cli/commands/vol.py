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

from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.vol import render_vol
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)
from schwab_cli.service.vol import NoVolData, VolStorageError, get_vol
from schwab_cli.ticker import TickerError, resolve as resolve_ticker


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
    # backfill notice goes to stderr, also human-mode only.
    human = not (as_json or as_md or snapshot_only)
    progress = (
        (lambda line: typer.secho(line, fg=typer.colors.CYAN)) if human else None
    )

    def _on_backfill_notice(n_synth: int) -> None:
        typer.secho(
            f"vol: backfilled {n_synth} synthetic IV days "
            f"for {ticker.underlying} from option + underlying history.",
            fg=typer.colors.CYAN,
            err=True,
        )

    on_backfill_notice = _on_backfill_notice if human else None

    try:
        result = get_vol(
            ticker.underlying,
            hv_window=hv_window,
            hv_lookback=hv_lookback,
            ivp_lookback=ivp_lookback,
            no_record=no_record,
            snapshot_only=snapshot_only,
            progress=progress,
            on_backfill_notice=on_backfill_notice,
        )
    except NotConfigured:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except NotAuthenticated:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except VolStorageError as e:
        typer.secho(str(e), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    except NoVolData as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except (ApiError, SessionExpired) as e:
        typer.secho(str(e) or type(e).__name__, fg=typer.colors.RED, err=True)
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
