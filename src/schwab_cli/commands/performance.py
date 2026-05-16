"""``schwab performance`` — chain-linked TWR for one or more accounts
over a date range, with SPX / COMP / RUT index comparison.

Math: :mod:`schwab_cli.analytics.twr`.
Data:
- positions, cash:       :func:`api.accounts.list_accounts`
- transactions:          :func:`api.transactions_cache.fetch_cached`
- daily closes:          ``ohlcv_daily`` cache + Schwab pricehistory fallback
- index closes:          ``$SPX`` / ``$COMPX`` / ``$RUT`` via pricehistory

Limitations of v1:
- Option positions are excluded from valuation (no OHLCV cache for OSI
  symbols). Their cash impact is still captured via trade fills.
- Money-market funds / sweep cash equivalents are treated as cash, not
  positions, regardless of how Schwab classifies them — the position
  payload typically already lumps them into ``cashBalance``.
- Holidays / missing close days forward-fill the prior trading day's
  close so a market closure doesn't create a value-zero ghost.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.twr import (
    DailyNav,
    DailyState,
    chain_link,
    reconstruct_history,
    simple_return,
)
from schwab_cli.api.accounts import list_accounts
from schwab_cli.api.client import SchwabClient
from schwab_cli.api.history import get_history
from schwab_cli.api.transactions_cache import fetch_cached as fetch_txns
from schwab_cli.commands.history import _cache_api_response
from schwab_cli.history_spec import RangeSpecError, parse_range
from schwab_cli.session import load as load_session
from schwab_cli.storage import ohlcv_history, vol_history


_NY = ZoneInfo("America/New_York")


INDEX_SYMBOLS: list[tuple[str, str]] = [
    # (display, Schwab symbol). Each is a real tradable index ticker on
    # Schwab's pricehistory endpoint — no constituent walk required.
    ("SPX",  "$SPX"),
    ("COMP", "$COMPX"),
    ("RUT",  "$RUT"),
]


@dataclass
class AccountReturn:
    account_number: str
    twr: float
    start_value: float
    end_value: float
    net_flow: float  # signed; positive = net deposit over the period
    # Heuristic — when this is True the account had option positions
    # at any point. Surfaced in the output so the user knows TWR is
    # equity-only and may diverge from the broker's number.
    has_options: bool = False


# ---- entry point -------------------------------------------------------


def run(*, range_str: str, account: str | None, as_json: bool) -> None:
    try:
        start_dt, end_dt = parse_range(range_str)
    except RangeSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2 if e.kind == "invalid" else 1)

    start_day = start_dt.astimezone(_NY).date()
    end_day   = end_dt.astimezone(_NY).date()
    if end_day < start_day:
        typer.secho("range end before start", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = _client()

    typer.echo(
        f"Range: {start_day} → {end_day} ({(end_day - start_day).days}d)",
        err=True,
    )

    raw_accounts = list_accounts(client)
    selected = _filter_accounts(raw_accounts, account=account)
    if not selected:
        typer.secho(
            "no matching accounts" if account else "no accounts available",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    account_results: list[AccountReturn] = []
    for item in selected:
        sec = item.get("securitiesAccount", {}) or {}
        acct_no = sec.get("accountNumber") or ""
        acct_hash = sec.get("hashValue") or _resolve_hash(client, acct_no)
        result = _compute_account(
            client,
            account_number=acct_no,
            account_hash=acct_hash,
            sec=sec,
            start_day=start_day,
            end_day=end_day,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        account_results.append(result)

    index_returns = _compute_index_returns(
        client, start_day=start_day, end_day=end_day,
    )

    if as_json:
        _emit_json(account_results, index_returns,
                   start_day=start_day, end_day=end_day)
    else:
        _emit_table(account_results, index_returns,
                    start_day=start_day, end_day=end_day)


# ---- account TWR -------------------------------------------------------


def _compute_account(
    client: SchwabClient,
    *,
    account_number: str,
    account_hash: str,
    sec: dict,
    start_day: date,
    end_day: date,
    start_dt: datetime,
    end_dt: datetime,
) -> AccountReturn:
    today_cash = float(
        (sec.get("currentBalances") or {}).get("cashBalance") or 0.0
    )
    today_positions = _extract_equity_positions(sec)

    txns = fetch_txns(
        client, account_number, start=start_dt, end=end_dt, refresh=False,
    )
    today = datetime.now(tz=_NY).date()
    trading_days = _trading_days(start_day, end_day)
    needed_anchor_days = trading_days + [today]

    states = reconstruct_history(
        today=today,
        today_positions=today_positions,
        today_cash=today_cash,
        transactions=txns,
        days=needed_anchor_days,
    )

    # Pull closes for every symbol that appeared at any point in the
    # reconstructed history. Lazy-fills the cache for unknowns.
    all_symbols = set()
    for s in states:
        all_symbols.update(s.positions.keys())
    closes_by_symbol = _ensure_closes(
        client, sorted(all_symbols), start=start_day, end=end_day,
    )

    navs = _states_to_navs(states, closes=closes_by_symbol, days=trading_days)
    if not navs:
        return AccountReturn(
            account_number=account_number,
            twr=0.0, start_value=0.0, end_value=0.0, net_flow=0.0,
        )
    has_options = any(
        " " in s or len(s) > 6  # OSI symbols look like "NVDA  260116C00200000"
        for s in all_symbols
    )
    return AccountReturn(
        account_number=account_number,
        twr=chain_link(navs),
        start_value=navs[0].value,
        end_value=navs[-1].value,
        net_flow=sum(n.external_flow for n in navs),
        has_options=has_options,
    )


def _extract_equity_positions(sec: dict) -> dict[str, float]:
    """Sum ``longQuantity − shortQuantity`` per symbol in the
    securitiesAccount payload.

    Option positions are included here — their close-price won't be in
    the cache so they contribute $0 to NAV — but tracking them keeps
    the position deltas applied by ``reconstruct_history`` symmetric.
    Excluding option positions today while still applying option-trade
    deltas to cash creates a cash-side asymmetry that inflates start-of-
    period NAV when the account has significant option premium flow.
    """
    out: dict[str, float] = {}
    for pos in (sec.get("positions") or []):
        inst = pos.get("instrument") or {}
        sym = inst.get("symbol")
        atype = (inst.get("assetType") or "").upper()
        if not sym or atype == "CURRENCY":
            continue
        try:
            qty = float(pos.get("longQuantity") or 0) - float(
                pos.get("shortQuantity") or 0
            )
        except (TypeError, ValueError):
            qty = 0.0
        if qty != 0.0:
            out[sym] = out.get(sym, 0.0) + qty
    return out


def _states_to_navs(
    states: list[DailyState],
    *,
    closes: dict[str, dict[date, float]],
    days: list[date],
) -> list[DailyNav]:
    """Price each daily state and emit NAV records for the trading-day
    series. Forward-fill missing closes from the prior trading day."""
    states_by_day = {s.day: s for s in states}
    last_close: dict[str, float] = {}
    out: list[DailyNav] = []
    for d in days:
        st = states_by_day.get(d)
        if st is None:
            continue
        value = st.cash
        for sym, qty in st.positions.items():
            c = closes.get(sym, {}).get(d)
            if c is not None:
                last_close[sym] = c
            elif sym in last_close:
                c = last_close[sym]
            else:
                continue  # no close for this day or earlier — skip
            value += qty * c
        out.append(DailyNav(day=d, value=value, external_flow=st.external_flow))
    return out


# ---- close-price fetching ---------------------------------------------


def _ensure_closes(
    client: SchwabClient,
    symbols: list[str],
    *,
    start: date,
    end: date,
) -> dict[str, dict[date, float]]:
    """{symbol: {day: close}} covering ``[start, end]``. Cache-first;
    falls back to Schwab pricehistory for any symbol whose cache doesn't
    reach back to ``start``. Symbols whose API fetch errors are skipped
    silently — their contribution to NAV simply degrades."""
    out: dict[str, dict[date, float]] = {}
    if not symbols:
        return out
    needs_fetch: list[str] = []
    with vol_history.connect() as conn:
        for sym in symbols:
            rows = ohlcv_history.read_range(
                conn, symbol=sym, start=start, end=end,
            )
            out[sym] = {
                date.fromisoformat(r["day"]): float(r["close"])
                for r in rows
            }
            earliest = (
                date.fromisoformat(rows[0]["day"]) if rows else None
            )
            if earliest is None or earliest > start:
                needs_fetch.append(sym)

    if not needs_fetch:
        return out

    from rich.console import Console
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TextColumn, TimeElapsedColumn,
    )
    err = Console(stderr=True)
    err.print(
        f"[dim]Backfilling {len(needs_fetch)} symbol(s) from Schwab…[/dim]"
    )
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[symbol]}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=err, transient=False,
    ) as progress:
        task = progress.add_task(
            "OHLCV backfill", total=len(needs_fetch), symbol="",
        )
        for sym in needs_fetch:
            progress.update(task, symbol=sym)
            try:
                raw = get_history(
                    client, sym,
                    frequency_type="daily", frequency=1,
                    start=_at_midnight_utc(start),
                    end=_at_midnight_utc(end + timedelta(days=1)),
                )
            except Exception as e:
                progress.console.print(
                    f"  [yellow]{sym}: skipped ({type(e).__name__})[/yellow]"
                )
                progress.advance(task)
                continue
            _cache_api_response(sym, raw)
            with vol_history.connect() as conn:
                rows = ohlcv_history.read_range(
                    conn, symbol=sym, start=start, end=end,
                )
            out[sym] = {
                date.fromisoformat(r["day"]): float(r["close"])
                for r in rows
            }
            progress.advance(task)
    return out


# ---- indices -----------------------------------------------------------


def _compute_index_returns(
    client: SchwabClient, *, start_day: date, end_day: date,
) -> dict[str, float]:
    out: dict[str, float] = {}
    for display, schwab_sym in INDEX_SYMBOLS:
        try:
            raw = get_history(
                client, schwab_sym,
                frequency_type="daily", frequency=1,
                start=_at_midnight_utc(start_day - timedelta(days=7)),
                end=_at_midnight_utc(end_day + timedelta(days=1)),
            )
        except Exception as e:
            typer.secho(
                f"{display}: index fetch failed ({type(e).__name__})",
                fg=typer.colors.YELLOW, err=True,
            )
            continue
        candles = raw.get("candles") or []
        if not candles:
            continue
        start_close = _closest_close(candles, start_day, prefer="forward")
        end_close   = _closest_close(candles, end_day,   prefer="backward")
        if start_close is None or end_close is None:
            continue
        out[display] = simple_return(start_close, end_close)
    return out


def _closest_close(
    candles: list[dict], target: date, *, prefer: str,
) -> float | None:
    """Pick the closing price for ``target``, falling forward or back
    to the nearest trading day if ``target`` itself was a holiday."""
    pairs: list[tuple[date, float]] = []
    for c in candles:
        ms = c.get("datetime")
        if ms is None:
            continue
        try:
            d = datetime.fromtimestamp(
                int(ms) / 1000, tz=timezone.utc,
            ).astimezone(_NY).date()
            pairs.append((d, float(c["close"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not pairs:
        return None
    pairs.sort()
    if prefer == "forward":
        for d, close in pairs:
            if d >= target:
                return close
        return pairs[-1][1]
    # backward (default): nearest <= target
    last: float | None = None
    for d, close in pairs:
        if d > target:
            break
        last = close
    return last if last is not None else pairs[0][1]


# ---- helpers -----------------------------------------------------------


def _trading_days(start: date, end: date) -> list[date]:
    """All weekdays in [start, end] inclusive. Holidays are handled by
    the forward-fill in ``_states_to_navs`` — emitting a weekday with
    no close just reuses the prior day's close, which is identical to
    skipping it for chain-link purposes."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def _at_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _filter_accounts(payload, *, account: str | None) -> list[dict]:
    if not isinstance(payload, list):
        return []
    if account is None:
        return payload
    needle = account.strip()
    return [
        item for item in payload
        if _matches_account(item, needle)
    ]


def _matches_account(item: dict, needle: str) -> bool:
    sec = item.get("securitiesAccount", {}) or {}
    acct = sec.get("accountNumber") or ""
    return acct == needle or acct.endswith(needle)


def _resolve_hash(client: SchwabClient, account_number: str) -> str:
    try:
        return client.resolve_account(account_number).hash_value
    except Exception:
        return ""


def _client() -> SchwabClient:
    cfg = config_module.load()
    session = load_session()
    if cfg is None or session is None:
        typer.secho(
            "No auth — run `schwab setup` + `schwab auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    if session.refresh_token_expires_at <= int(time.time()):
        typer.secho("Refresh token expired. Run `schwab auth --force`.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


# ---- rendering ---------------------------------------------------------


def _emit_table(
    accounts: list[AccountReturn],
    indices: dict[str, float],
    *, start_day: date, end_day: date,
) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(
        title=(
            f"Performance (TWR) — {start_day} → {end_day}"
        ),
        title_justify="left",
        padding=(0, 1),
    )
    table.add_column("Entity",   no_wrap=True)
    table.add_column("Return",   justify="right")
    table.add_column("Gain $",   justify="right")
    table.add_column("Start $",  justify="right", style="dim")
    table.add_column("End $",    justify="right", style="dim")
    table.add_column("Net Flow", justify="right", style="dim")

    any_options = False
    for a in accounts:
        any_options = any_options or a.has_options
        suffix = a.account_number[-4:] if len(a.account_number) >= 4 \
            else a.account_number
        label = f"acct …{suffix}"
        if a.has_options:
            label += " *"
        gain = a.end_value - a.start_value - a.net_flow
        table.add_row(
            label,
            _pct_color(a.twr),
            _money_color(gain),
            _money(a.start_value),
            _money(a.end_value),
            _money(a.net_flow),
        )
    if accounts:
        table.add_row("", "", "", "", "", "")
    for code, _sym in INDEX_SYMBOLS:
        r = indices.get(code)
        if r is None:
            table.add_row(code, "—", "", "", "", "")
        else:
            table.add_row(code, _pct_color(r), "", "", "", "")
    console = Console(width=110, soft_wrap=False)
    console.print(table)
    if any_options:
        console.print(
            "[dim]* equity-only valuation — options contribute $0 to "
            "NAV (no OHLCV cache for option symbols). TWR for "
            "option-heavy accounts may diverge from the broker's "
            "official number.[/dim]"
        )


def _pct_color(v: float) -> str:
    color = "green" if v > 0 else ("red" if v < 0 else "white")
    return f"[{color}]{v * 100:+.2f}%[/{color}]"


def _money(v: float) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _money_color(v: float) -> str:
    if v is None:
        return "—"
    color = "green" if v > 0 else ("red" if v < 0 else "white")
    return f"[{color}]${v:+,.0f}[/{color}]"


def _emit_json(
    accounts: list[AccountReturn],
    indices: dict[str, float],
    *, start_day: date, end_day: date,
) -> None:
    import json as _json
    typer.echo(_json.dumps({
        "range": {"start": start_day.isoformat(), "end": end_day.isoformat()},
        "accounts": [
            {
                "account_number": a.account_number,
                "twr": a.twr,
                "start_value": a.start_value,
                "end_value": a.end_value,
                "net_flow": a.net_flow,
            } for a in accounts
        ],
        "indices": {k: v for k, v in indices.items()},
    }, indent=2))
