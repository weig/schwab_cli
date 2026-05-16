"""``schwab breadth`` — market-breadth table.

Renders, for each configured index, the percentage of constituents
trading above their N-period SMA across Bloomberg-style timeframes
(5D, 10D, 30D, 60D, 90D, 180D, 48W, 96W, 1Y, 2Y).

Data sources:
- Constituent lists: ``schwab_cli.dataset.indices.fetch_index_members``
  (stockanalysis.com primary, SSGA fallback).
- Closes: ``ohlcv_daily`` cache, with on-demand API backfill via
  ``api.history.get_history`` for any missing suffix.
"""
from __future__ import annotations

import json as _json
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx
import typer

from schwab_cli.analytics.breadth import (
    MAX_WINDOW,
    TIMEFRAMES,
    BreadthCell,
    compute_breadth,
)
from schwab_cli.api.client import SchwabClient
from schwab_cli.api.history import get_history
from schwab_cli.commands.history import _cache_api_response
from schwab_cli import config as config_module
from schwab_cli.dataset.indices import fetch_index_members
from schwab_cli.session import load as load_session
from schwab_cli.storage import ohlcv_history, vol_history


# Bloomberg's "COMP" is the Nasdaq Composite (~3000 stocks), which has
# no clean upstream feed. NQ (Nasdaq 100) is the closest cleanly
# available proxy and is what every other tool in this CLI already
# tracks. Label it honestly so the operator isn't surprised.
INDEX_LABELS: dict[str, str] = {
    "SPX": "SPX (S&P 500)",
    "NQ": "NQ  (Nasdaq 100)",
    "DJI": "DJI (Dow Jones)",
}
DEFAULT_INDICES: list[str] = ["SPX", "NQ", "DJI"]

_NY = ZoneInfo("America/New_York")
# Calendar-day buffer over the trading-day window — weekends + holidays
# mean ~252 trading days fit in ~365 calendar days. 1.5x is plenty.
_CALENDAR_DAYS_PER_WINDOW = 1.5


def run(
    *,
    indices: list[str] | None = None,
    as_json: bool = False,
    refresh_members: bool = False,
) -> None:
    """Entry point invoked by the CLI. Lazy-fills the OHLCV cache for
    any constituent missing recent daily candles, then computes and
    renders the breadth table."""
    indices = indices or DEFAULT_INDICES
    for idx in indices:
        if idx not in INDEX_LABELS:
            typer.secho(
                f"Unsupported index {idx!r}. "
                f"Supported: {sorted(INDEX_LABELS)}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=2)

    today_ny = datetime.now(tz=_NY).date()
    start = today_ny - timedelta(
        days=int(MAX_WINDOW * _CALENDAR_DAYS_PER_WINDOW),
    )

    members_by_index = _resolve_members(indices, refresh=refresh_members)
    all_symbols = sorted({s for syms in members_by_index.values() for s in syms})

    typer.echo(
        f"Loading OHLCV for {len(all_symbols)} symbols "
        f"({start} → {today_ny})…",
        err=True,
    )
    closes_by_symbol = _load_closes(
        all_symbols, start=start, end=today_ny,
    )

    rows: list[tuple[str, list[BreadthCell]]] = []
    for idx in indices:
        members = members_by_index[idx]
        idx_closes = {
            s: closes_by_symbol[s]
            for s in members
            if s in closes_by_symbol and closes_by_symbol[s]
        }
        cells = [
            compute_breadth(closes_by_symbol=idx_closes, window=n)
            for _, n in TIMEFRAMES
        ]
        rows.append((idx, cells))

    if as_json:
        typer.echo(_render_json(rows, asof=today_ny))
    else:
        _render_table(rows, asof=today_ny)


# ---- members & closes -------------------------------------------------


def _resolve_members(
    indices: Iterable[str], *, refresh: bool,
) -> dict[str, set[str]]:
    """Pull index → constituent set. Always live for now — the upstream
    pages are cheap and constituent churn is meaningful for breadth
    accuracy. ``refresh`` is a hook for a future cache layer."""
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn

    err_console = Console(stderr=True)
    indices_list = list(indices)
    out: dict[str, set[str]] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        console=err_console,
        transient=True,
    ) as progress:
        with httpx.Client(timeout=30.0) as client:
            for idx in indices_list:
                task = progress.add_task(
                    f"Fetching {idx} constituents…", total=None,
                )
                out[idx] = fetch_index_members(idx, client=client)
                progress.update(
                    task,
                    description=f"{idx}: {len(out[idx])} members",
                )
                progress.remove_task(task)
    return out


def _load_closes(
    symbols: list[str], *, start: date, end: date,
) -> dict[str, list[float]]:
    """Return ``{symbol: closes_asc}`` for every requested symbol.

    Pulls from ``ohlcv_daily`` first; for any symbol whose cache
    doesn't cover ``end``, falls back to Schwab's pricehistory and
    seeds the cache. Symbols that fail to fetch are skipped (logged)
    so one delisted ticker can't blow up the whole table.
    """
    # First sweep: cache only — fast path for warm runs.
    # Need-fetch is true when (a) suffix gap exists OR (b) the cache
    # doesn't reach back to ``start``. Breadth needs 2Y of history per
    # symbol; the daily cron only seeds the latest day, so a fresh
    # install will trip case (b) for every constituent.
    with vol_history.connect() as conn:
        gaps: list[tuple[str, date, date]] = []
        closes: dict[str, list[float]] = {}
        for sym in symbols:
            rows = ohlcv_history.read_range(
                conn, symbol=sym, start=start, end=end,
            )
            closes[sym] = [float(r["close"]) for r in rows]
            earliest = date.fromisoformat(rows[0]["day"]) if rows else None
            suffix_gap = ohlcv_history.gap(
                conn, symbol=sym, start=start, end=end,
            )
            insufficient_prefix = earliest is None or earliest > start
            if suffix_gap is not None or insufficient_prefix:
                # Always fetch the full range — Schwab returns it cheap
                # and the upsert is idempotent, so we don't need to
                # merge prefix + suffix windows.
                gaps.append((sym, start, end))

    if not gaps:
        return closes

    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    err_console = Console(stderr=True)
    err_console.print(
        f"[dim]Cache miss for {len(gaps)} symbols — "
        f"backfilling via Schwab…[/dim]"
    )

    client = _schwab_client()
    failures: list[str] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[symbol]}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=err_console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            "Backfilling OHLCV", total=len(gaps), symbol="",
        )
        for sym, g_start, g_end in gaps:
            progress.update(task, symbol=sym)
            try:
                raw = get_history(
                    client,
                    sym,
                    frequency_type="daily",
                    frequency=1,
                    start=_at_midnight_utc(g_start),
                    end=_at_midnight_utc(g_end + timedelta(days=1)),
                )
            except Exception as e:
                progress.console.print(
                    f"  [yellow]{sym}: skipped "
                    f"({type(e).__name__}: {e})[/yellow]"
                )
                failures.append(sym)
                progress.advance(task)
                continue
            _cache_api_response(sym, raw)
            with vol_history.connect() as conn:
                rows = ohlcv_history.read_range(
                    conn, symbol=sym, start=start, end=end,
                )
            closes[sym] = [float(r["close"]) for r in rows]
            progress.advance(task)

    if failures:
        err_console.print(
            f"[yellow]Skipped {len(failures)} symbols: "
            f"{', '.join(failures[:10])}"
            f"{'…' if len(failures) > 10 else ''}[/yellow]"
        )
    return closes


def _at_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _schwab_client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


# ---- rendering --------------------------------------------------------


def _fmt_pct(cell: BreadthCell) -> str:
    if cell.pct is None:
        return "—"
    return f"{cell.pct * 100:5.1f}%"


def _render_table(
    rows: list[tuple[str, list[BreadthCell]]], *, asof: date,
) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(
        title=f"% of constituents above SMA — as of {asof.isoformat()}",
        title_justify="left",
        padding=(0, 1),
    )
    table.add_column("Index", no_wrap=True)
    for label, _ in TIMEFRAMES:
        table.add_column(label, justify="right", no_wrap=True)
    table.add_column("n", justify="right", no_wrap=True)

    for idx, cells in rows:
        values = [_fmt_pct(c) for c in cells]
        min_counted = min((c.counted for c in cells), default=0)
        total = cells[0].total if cells else 0
        table.add_row(
            INDEX_LABELS[idx], *values, f"{min_counted}/{total}",
        )
    # Force a wide-enough console — the 12-column table needs ~100 cols
    # to render without per-cell truncation, but rich detects an 80-col
    # default when stdout is piped which produces ugly "5…" cells.
    Console(width=140, soft_wrap=False).print(table)


def _render_json(
    rows: list[tuple[str, list[BreadthCell]]], *, asof: date,
) -> str:
    payload = {
        "asof": asof.isoformat(),
        "indices": [
            {
                "code": idx,
                "label": INDEX_LABELS[idx],
                "timeframes": [
                    {
                        "label": label,
                        "window_days": window,
                        "pct_above_ma": cell.pct,
                        "counted": cell.counted,
                        "total": cell.total,
                    }
                    for (label, window), cell in zip(TIMEFRAMES, cells)
                ],
            }
            for idx, cells in rows
        ],
    }
    return _json.dumps(payload, indent=2)
