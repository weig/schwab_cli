"""``schwab performance`` — Schwab-style performance decomposition.

Layout matches Schwab's "Change Factor" report:

    Beginning Value
    Net Contributions      ( Contributions + Withdrawals )
    Investment Changes     ( Realized + Unrealized + Income − Fees )
    Ending Value

All numbers come from primary-source data:

- Ending Value: today's account API payload (cash + marketValue per position).
- Inflows / Outflows: ``netAmount`` of pure-cash external transactions
  (no security legs) in the period.
- Income: ``netAmount`` for ``DIVIDEND_OR_INTEREST`` transactions.
- Fees: ``cost`` summed across all fee legs.
- Realized P&L: FIFO lot matching across in-period TRADE transactions
  (closes against opens that ALSO sit in the period; orphan closes are
  skipped — their cost basis predates the window).
- Unrealized P&L change: derived as residual so identity holds::

      BV + NetContrib + Realized + Unrealized + Income + Fees = EV

  This makes the table reconcile exactly even when individual sub-totals
  are approximate. The Beginning Value used in this identity is itself
  computed via position reconstruction (equity-only — options can't be
  priced historically), so the Unrealized component absorbs the option
  valuation error. The asterisked footnote surfaces this caveat when
  any option position touched the account.

Index comparison (SPX / COMP / RUT) uses simple point-to-point returns
on the index pricehistory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.twr import (
    DailyState,
    classify_transactions,
    realized_pl_fifo,
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
    ("SPX",  "$SPX"),
    ("COMP", "$COMPX"),
    ("RUT",  "$RUT"),
]


@dataclass
class AccountPerformance:
    """One row of the Schwab-style decomposition. All values in
    period-local dollars. ``unrealized`` is the residual that makes
    ``BV + NetContrib + Realized + Unrealized + Income + Fees == EV``
    hold exactly."""
    account_number: str
    beginning_value: float
    ending_value: float
    inflow: float
    outflow: float
    realized: float
    unrealized: float
    income: float
    fees: float
    has_options: bool

    @property
    def net_contrib(self) -> float:
        return self.inflow + self.outflow  # outflow stored as negative

    @property
    def total_gain(self) -> float:
        return self.realized + self.unrealized + self.income + self.fees


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

    results: list[AccountPerformance] = []
    for item in selected:
        sec = item.get("securitiesAccount", {}) or {}
        results.append(_compute_account(
            client, sec=sec,
            start_day=start_day, end_day=end_day,
            start_dt=start_dt, end_dt=end_dt,
        ))

    index_returns = _compute_index_returns(
        client, start_day=start_day, end_day=end_day,
    )

    if as_json:
        _emit_json(results, index_returns,
                   start_day=start_day, end_day=end_day)
    else:
        _emit_table(results, index_returns,
                    start_day=start_day, end_day=end_day)


# ---- per-account decomposition ----------------------------------------


def _compute_account(
    client: SchwabClient,
    *,
    sec: dict,
    start_day: date,
    end_day: date,
    start_dt: datetime,
    end_dt: datetime,
) -> AccountPerformance:
    acct_no = sec.get("accountNumber") or ""

    end_cash, end_market_value = _ending_value_components(sec)
    end_value = end_cash + end_market_value

    today_positions = _extract_positions(sec)
    has_options = any(_looks_like_option(s) for s in today_positions)

    txns = fetch_txns(
        client, acct_no, start=start_dt, end=end_dt, refresh=False,
    )
    buckets = classify_transactions(txns)
    realized = realized_pl_fifo(txns)

    today = datetime.now(tz=_NY).date()
    needed_days = sorted({start_day, today})
    states = reconstruct_history(
        today=today,
        today_positions=today_positions,
        today_cash=end_cash,
        transactions=txns,
        days=needed_days,
    )
    closes_by_symbol = _ensure_closes(
        client, sorted(today_positions.keys()),
        start=start_day, end=end_day,
    )
    avg_price = _avg_price_by_symbol(sec)
    beginning_value = _price_state(
        _find_state(states, start_day),
        closes=closes_by_symbol,
        avg_price=avg_price,
    )

    net_contrib = buckets["inflow"] + buckets["outflow"]
    investment_change = end_value - beginning_value - net_contrib
    unrealized = (
        investment_change - realized - buckets["income"] - buckets["fees"]
    )

    return AccountPerformance(
        account_number=acct_no,
        beginning_value=beginning_value,
        ending_value=end_value,
        inflow=buckets["inflow"],
        outflow=buckets["outflow"],
        realized=realized,
        unrealized=unrealized,
        income=buckets["income"],
        fees=buckets["fees"],
        has_options=has_options,
    )


def _ending_value_components(sec: dict) -> tuple[float, float]:
    cash = float(
        (sec.get("currentBalances") or {}).get("cashBalance") or 0.0
    )
    mv = 0.0
    for pos in (sec.get("positions") or []):
        try:
            mv += float(pos.get("marketValue") or 0.0)
        except (TypeError, ValueError):
            pass
    return cash, mv


def _extract_positions(sec: dict) -> dict[str, float]:
    """All open positions (equity + option) as ``{symbol: signed_qty}``.
    Options are tracked here so trade reversals in
    ``reconstruct_history`` cancel symmetrically — they price at $0
    historically (no OHLCV) and the residual ``unrealized`` term
    absorbs the gap."""
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


def _looks_like_option(symbol: str) -> bool:
    # OSI symbols are fixed-width with spaces, e.g.
    # "NVDA  260116C00200000". Anything with a space or longer than 6
    # chars is almost certainly an option.
    return " " in symbol or len(symbol) > 6


def _avg_price_by_symbol(sec: dict) -> dict[str, float]:
    """Map ``symbol → averagePrice`` from the current positions payload.

    Used as a historical-valuation fallback for option symbols with no
    OHLCV cache: cost basis × qty × contract-multiplier is a far better
    proxy than $0 for positions that existed at the start of the
    period. The approximation: a position's value at start_day is
    treated as its lifetime cost basis. For options held all the way
    through the period this materially closes the Beginning Value gap
    against Schwab's official number.
    """
    out: dict[str, float] = {}
    for pos in (sec.get("positions") or []):
        inst = pos.get("instrument") or {}
        sym = inst.get("symbol")
        if not sym:
            continue
        try:
            avg = float(pos.get("averagePrice") or 0.0)
        except (TypeError, ValueError):
            continue
        if avg > 0:
            out[sym] = avg
    return out


def _find_state(states, day: date) -> DailyState:
    for s in states:
        if s.day == day:
            return s
    for s in states:
        if s.day >= day:
            return s
    return states[0] if states else DailyState(
        day=day, positions={}, cash=0.0, external_flow=0.0,
    )


def _price_state(
    state: DailyState,
    *,
    closes: dict[str, dict[date, float]],
    avg_price: dict[str, float] | None = None,
) -> float:
    """Value a position+cash snapshot using daily closes.

    Fallback chain per symbol:
    1. closing price for ``state.day``
    2. nearest earlier close in the cache
    3. ``avg_price[symbol]`` × contract multiplier (option positions
       have no OHLCV cache — cost basis is a sane non-zero proxy)
    4. $0 (contribute nothing)
    """
    value = state.cash
    avg_price = avg_price or {}
    for sym, qty in state.positions.items():
        price: float | None = None
        sym_closes = closes.get(sym) or {}
        if sym_closes:
            price = sym_closes.get(state.day)
            if price is None:
                earlier = [d for d in sym_closes if d <= state.day]
                if earlier:
                    price = sym_closes[max(earlier)]
        if price is None and sym in avg_price:
            # Option marketValue uses a 100× multiplier in Schwab's
            # payload. Equity averagePrice is per share (multiplier 1).
            mult = 100.0 if _looks_like_option(sym) else 1.0
            price = avg_price[sym] * mult
        if price is None:
            continue
        value += qty * price
    return value


# ---- close-price fetching ---------------------------------------------


def _ensure_closes(
    client: SchwabClient,
    symbols: list[str],
    *,
    start: date,
    end: date,
) -> dict[str, dict[date, float]]:
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
    err.print(f"[dim]Backfilling {len(needs_fetch)} symbol(s)…[/dim]")
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
            except Exception:
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


def _closest_close(candles, target, *, prefer):
    pairs = []
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
        for d, c in pairs:
            if d >= target:
                return c
        return pairs[-1][1]
    last = None
    for d, c in pairs:
        if d > target:
            break
        last = c
    return last if last is not None else pairs[0][1]


# ---- helpers -----------------------------------------------------------


def _at_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _filter_accounts(payload, *, account):
    if not isinstance(payload, list):
        return []
    if account is None:
        return payload
    needle = account.strip()
    return [
        item for item in payload
        if (item.get("securitiesAccount", {}) or {})
              .get("accountNumber", "").endswith(needle)
    ]


def _client():
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
    accounts: list[AccountPerformance],
    indices: dict[str, float],
    *, start_day: date, end_day: date,
) -> None:
    from rich.console import Console
    console = Console(width=110, soft_wrap=False)
    for a in accounts:
        _render_account_block(console, a,
                              start_day=start_day, end_day=end_day)
        console.print("")
    _render_index_block(console, indices)


def _render_account_block(
    console, a: AccountPerformance,
    *, start_day: date, end_day: date,
) -> None:
    from rich.table import Table

    suffix = a.account_number[-4:] if len(a.account_number) >= 4 \
        else a.account_number
    title = (
        f"Performance Decomposition — acct …{suffix} | "
        f"{start_day} → {end_day}"
    )
    table = Table(
        title=title, title_justify="left", padding=(0, 1),
        show_header=False,
    )
    table.add_column("Change Factor", no_wrap=True)
    table.add_column("Amount", justify="right")

    table.add_row(
        "[bold]Beginning Value[/bold]",
        f"[bold]{_money(a.beginning_value)}[/bold]",
    )
    table.add_row("", "")
    table.add_row(
        "[bold]Net Contributions[/bold]",
        f"[bold]{_money_signed(a.net_contrib)}[/bold]",
    )
    table.add_row("  Contributions",  _money_signed(a.inflow, color=True))
    table.add_row("  Withdrawals",    _money_signed(a.outflow, color=True))
    table.add_row("", "")
    table.add_row(
        "[bold]Investment Changes[/bold]",
        f"[bold]{_money_signed(a.total_gain, color=True)}[/bold]",
    )
    table.add_row(
        "  Realized Gain/Loss",   _money_signed(a.realized,   color=True))
    table.add_row(
        "  Unrealized Gain/Loss", _money_signed(a.unrealized, color=True))
    table.add_row(
        "  Income (div + int)",   _money_signed(a.income,     color=True))
    table.add_row(
        "  Fees & Expenses",      _money_signed(a.fees,       color=True))
    table.add_row("", "")
    table.add_row(
        "[bold]Ending Value[/bold]",
        f"[bold]{_money(a.ending_value)}[/bold]",
    )
    console.print(table)

    # Period return % is only meaningful when Beginning Value is
    # reliable. Option-heavy accounts have an equity-only BV (no OHLCV
    # cache for OSI symbols), so the % blows up and would mislead the
    # operator. Show $-gain prominently; suppress the % with a note.
    if a.has_options:
        console.print(
            f"  [bold]Total Gain:[/bold] "
            f"{_money_signed(a.total_gain, color=True)}   "
            f"[dim](% return suppressed — Beginning Value is "
            f"equity-only for option-bearing accounts; see "
            f"Unrealized residual above)[/dim]"
        )
    else:
        denominator = a.beginning_value + a.net_contrib / 2
        pct = (a.total_gain / denominator) if denominator > 0 else None
        if pct is not None:
            console.print(
                f"  [bold]Period Return:[/bold] {_pct_color(pct)}   "
                f"[dim](modified-Dietz)[/dim]"
            )


def _render_index_block(console, indices):
    from rich.table import Table
    if not indices:
        return
    table = Table(
        title="Benchmark Returns (same range)",
        title_justify="left", padding=(0, 1), show_header=False,
    )
    table.add_column("Index", no_wrap=True)
    table.add_column("Return", justify="right")
    for code, _sym in INDEX_SYMBOLS:
        r = indices.get(code)
        table.add_row(code, _pct_color(r) if r is not None else "—")
    console.print(table)


def _money(v):
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _money_signed(v, *, color: bool = False) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    text = f"{sign}${abs(v):,.2f}"
    if not color:
        return text
    if v > 0:
        return f"[green]{text}[/green]"
    if v < 0:
        return f"[red]{text}[/red]"
    return text


def _pct_color(v):
    if v is None:
        return "—"
    color = "green" if v > 0 else ("red" if v < 0 else "white")
    return f"[{color}]{v * 100:+.2f}%[/{color}]"


def _emit_json(accounts, indices, *, start_day, end_day):
    import json as _json
    typer.echo(_json.dumps({
        "range": {"start": start_day.isoformat(),
                  "end":   end_day.isoformat()},
        "accounts": [
            {
                "account_number":   a.account_number,
                "beginning_value":  a.beginning_value,
                "ending_value":     a.ending_value,
                "inflow":           a.inflow,
                "outflow":          a.outflow,
                "net_contrib":      a.net_contrib,
                "realized":         a.realized,
                "unrealized":       a.unrealized,
                "income":           a.income,
                "fees":             a.fees,
                "total_gain":       a.total_gain,
                "has_options":      a.has_options,
            } for a in accounts
        ],
        "indices": indices,
    }, indent=2))
