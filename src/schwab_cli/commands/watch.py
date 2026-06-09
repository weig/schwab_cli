"""``schwab watch`` — manual ticker watchlist.

Subcommands:

- ``add SYMBOL``    — add to watchlist and subscribe to OHLCV + volatility.
- ``remove SYMBOL`` — remove from watchlist; if no other source covers
                      the symbol, demote ticker_state to GRACE so the
                      cron ages it out of data collection.
- ``list``          — table snapshot via REST /quotes.
- ``show``          — live streaming table via the Schwab streamer.

Storage: rows in ``subscriptions`` with ``source='watch'``. One row per
``(symbol, group_name)``. The OHLCV cron and volatility cron pick up
``source='watch'`` rows the same way they pick up indices/positions.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import SchwabClient
from schwab_cli.api.quotes import get_quotes
from schwab_cli.commands._stream_mcp import (
    DEFAULT_MCP_URL as _DEFAULT_MCP_URL,
)
from schwab_cli.commands._stream_mcp import (
    McpUnreachable,
    probe_daemon,
    stream_quotes_via_mcp,
)
from schwab_cli.session import load as load_session
from schwab_cli.storage import vol_history
from schwab_cli.storage.groups import GROUP_OHLCV, GROUP_VOLATILITY
_SERVICE = "LEVELONE_EQUITIES"


_NY = ZoneInfo("America/New_York")
_WATCH_GROUPS = (GROUP_OHLCV, GROUP_VOLATILITY)


# ---- add / remove ------------------------------------------------------


def run_add(symbol: str) -> None:
    """Subscribe ``symbol`` to both OHLCV and volatility groups under
    ``source='watch'``. Idempotent — re-adding revives a removed row."""
    from schwab_cli.dataset.store import subscribe_watch

    sym = _normalize(symbol)
    if not sym:
        typer.secho("symbol cannot be empty", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    with vol_history.connect() as conn:
        for g in _WATCH_GROUPS:
            subscribe_watch(conn, symbol=sym, group_name=g)
    typer.secho(
        f"watching {sym} (subscribed to {', '.join(_WATCH_GROUPS)})",
        fg=typer.colors.GREEN,
    )


def run_remove(symbol: str) -> None:
    """Soft-delete the watch rows. If the symbol has no other active
    source for a given group, demote ``ticker_state.tier`` to GRACE so
    the cron's tier evaluator ages it out of data collection.

    Indices subscriptions are not source='watch' — when a symbol is
    held by an index, the GRACE demotion still happens (no other source
    means none other than ``watch``), but the indices grace logic in
    ``list_active_subscriptions`` keeps sampling running through the
    index exit. Net effect: indices-covered symbols keep flowing data
    even after watch removal, exactly as the user asked.
    """
    from schwab_cli.dataset.store import (
        has_other_active_source,
        read_ticker_state,
        unsubscribe_watch,
        write_ticker_state,
    )

    sym = _normalize(symbol)
    if not sym:
        typer.secho("symbol cannot be empty", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    now_ms = int(time.time() * 1000)
    demoted: list[str] = []
    untouched: list[str] = []
    with vol_history.connect() as conn:
        # Check whether this symbol is a member of any active index —
        # the user's spec said "if not in indices, demote to GRACE".
        in_indices = _symbol_in_active_indices(conn, sym)

        for g in _WATCH_GROUPS:
            unsubscribe_watch(conn, symbol=sym, group_name=g)
            if in_indices:
                untouched.append(g)
                continue
            if has_other_active_source(
                conn, symbol=sym, group_name=g, exclude_source="watch",
            ):
                untouched.append(g)
                continue
            existing = read_ticker_state(conn, symbol=sym, group_name=g)
            write_ticker_state(
                conn, symbol=sym, group_name=g, tier="GRACE",
                tier_since=now_ms,
                consecutive_days_below=(
                    existing["consecutive_days_below"] if existing else 0
                ),
                last_evaluated_at=now_ms,
            )
            demoted.append(g)

    if demoted:
        typer.secho(
            f"unwatched {sym}; demoted to GRACE for: {', '.join(demoted)}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho(
            f"unwatched {sym}; still covered by other sources "
            f"({', '.join(untouched)})",
            fg=typer.colors.GREEN,
        )


def _symbol_in_active_indices(conn, symbol: str) -> bool:
    """True if ``symbol`` is currently a member of any active index
    subscription. We treat any indices-source row in ``subscriptions``
    as evidence of index membership — the indices cron writes one row
    per (symbol, group, index)."""
    row = conn.execute(
        """
        SELECT 1 FROM subscriptions
        WHERE symbol = ? AND source = 'indices' AND unsubscribed_at IS NULL
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return row is not None


# ---- list (one-shot) ---------------------------------------------------


def run_list(as_json: bool = False) -> None:
    """Render the watchlist with current quote data — bid/ask/sizes,
    volume, OHLC, last + change %. Hits Schwab REST /quotes; one shot,
    no streaming."""
    from schwab_cli.dataset.store import list_watched_symbols

    with vol_history.connect() as conn:
        symbols = list_watched_symbols(conn)
    if not symbols:
        typer.echo("(watchlist empty — use `schwab watch add SYMBOL`)")
        return

    client = _client()
    raw = get_quotes(client, symbols)
    snapshots = [_extract_quote(sym, raw.get(sym)) for sym in symbols]
    asof = datetime.now(tz=_NY)

    if as_json:
        import json as _json
        typer.echo(_json.dumps(
            {
                "asof": asof.isoformat(timespec="seconds"),
                "rows": [snap.as_dict() for snap in snapshots],
            },
            indent=2,
        ))
        return

    _render_table(snapshots, asof=asof, live=False)


# ---- show (live stream) -----------------------------------------------


def run_show(
    *,
    direct: bool = False,
    force: bool = False,
    mcp_url: str = _DEFAULT_MCP_URL,
) -> None:
    """Subscribe to LEVELONE_EQUITIES for every watchlist symbol and
    render an updating rich.Live table. ``Ctrl-C`` to exit.

    Prefers the daemon's shared streamer (Schwab allows one streamer per
    account): when a daemon is reachable, quotes come through it; otherwise
    we open our own direct connection. ``--direct`` forces a direct
    connection but is refused while a daemon is up (it would kick the
    daemon's session) unless ``--force``.
    """
    from schwab_cli.dataset.store import list_watched_symbols

    with vol_history.connect() as conn:
        symbols = list_watched_symbols(conn)
    if not symbols:
        typer.echo("(watchlist empty — use `schwab watch add SYMBOL`)")
        return

    if direct and probe_daemon(mcp_url) and not force:
        typer.secho(
            f"MCP daemon is running at {mcp_url} — refusing --direct because "
            "Schwab allows only one streamer session per account and a direct "
            "connection would disconnect the daemon.",
            fg=typer.colors.RED, err=True,
        )
        typer.secho(
            "Drop --direct to stream through the daemon, or pass "
            "--direct --force to proceed anyway.",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(code=2)

    use_mcp = not direct and probe_daemon(mcp_url)
    if use_mcp:
        try:
            asyncio.run(_run_show_via_mcp(symbols, mcp_url=mcp_url))
            return
        except McpUnreachable:
            # The MCP stream dropped. Only fall back to a direct streamer if
            # the daemon is actually gone — re-probe first. If it's still up,
            # a direct connection would kick its session (the collision this
            # routing exists to avoid), so abort instead of evicting it.
            if probe_daemon(mcp_url):
                typer.secho(
                    "MCP daemon is still running but its stream connection "
                    "failed — not opening a direct streamer (it would "
                    "disconnect the daemon). Retry, or use --direct --force "
                    "to override.",
                    fg=typer.colors.RED, err=True,
                )
                raise typer.Exit(code=1)
            typer.secho(
                "(MCP daemon went away; falling back to a direct streamer)",
                fg=typer.colors.YELLOW, err=True,
            )
        except KeyboardInterrupt:
            typer.echo("\n(stopped)")
            return

    try:
        asyncio.run(_run_show_direct(symbols))
    except KeyboardInterrupt:
        typer.echo("\n(stopped)")


def _apply_decoded(
    state: dict[str, "QuoteSnapshot"], decoded: dict[str, Any]
) -> bool:
    """Merge one decoded quote update into ``state``. Returns True if a
    watched symbol changed (so the caller repaints)."""
    sym = (decoded.get("symbol") or "").upper()
    if sym not in state:
        return False
    state[sym] = state[sym].merged_with(decoded)
    return True


def _seed_state(
    symbols: list[str],
) -> dict[str, "QuoteSnapshot"]:
    """Best-effort REST snapshot so the first paint isn't blank. Falls
    back to empty snapshots (filled by the first streaming frames) when
    auth or the quote call is unavailable — used by the MCP path, where
    the daemon (not this process) owns the streamer."""
    empty = {sym: QuoteSnapshot(symbol=sym) for sym in symbols}
    cfg = config_module.load()
    session = load_session()
    if cfg is None or session is None:
        return empty
    try:
        seed = get_quotes(SchwabClient(cfg, session), symbols)
    except Exception:  # noqa: BLE001 — best-effort seed
        return empty
    return {sym: _extract_quote(sym, seed.get(sym)) for sym in symbols}


async def _run_show_via_mcp(symbols: list[str], *, mcp_url: str) -> None:
    """Render the watch table from the daemon's shared streamer."""
    from rich.live import Live

    state = _seed_state(symbols)
    asof = {"t": datetime.now(tz=_NY)}
    with Live(
        _build_table(list(state.values()), asof=asof["t"], live=True),
        refresh_per_second=4,
        screen=False,
    ) as live:
        def on_decoded(decoded: dict[str, Any]) -> None:
            if _apply_decoded(state, decoded):
                asof["t"] = datetime.now(tz=_NY)
                live.update(_build_table(
                    list(state.values()), asof=asof["t"], live=True,
                ))

        await stream_quotes_via_mcp(
            symbols, mcp_url=mcp_url, on_decoded=on_decoded,
        )


async def _run_show_direct(symbols: list[str]) -> None:
    """Open our own streamer session, subscribe to ``symbols``, and update
    a rich.Live table on every data frame."""
    from rich.live import Live

    from schwab_cli.api.streamer import (
        Streamer,
        classify_frame,
        fetch_streamer_info,
        is_heartbeat,
    )
    from schwab_cli.api.streamer_fields import decode, default_fields

    cfg = config_module.load()
    session = load_session()
    if cfg is None or session is None:
        typer.secho(
            "No auth — run `schwab setup` + `schwab auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    client = SchwabClient(cfg, session)
    info = fetch_streamer_info(client)

    # Seed the table with a REST snapshot so the first frame's render
    # has all rows present (the streamer typically pushes one symbol at
    # a time — without a seed the first paint would have N-1 blanks).
    seed = get_quotes(client, symbols)
    state: dict[str, QuoteSnapshot] = {
        sym: _extract_quote(sym, seed.get(sym)) for sym in symbols
    }
    last_update = datetime.now(tz=_NY)

    streamer = Streamer(info, session.access_token)
    await streamer.connect()
    try:
        await streamer.login()
        await streamer.subscribe(
            service=_SERVICE,
            keys=[s.upper() for s in symbols],
            fields=default_fields(_SERVICE),
        )
        with Live(
            _build_table(list(state.values()), asof=last_update, live=True),
            refresh_per_second=4,
            screen=False,
        ) as live:
            async for frame in streamer.messages():
                if is_heartbeat(frame):
                    continue
                if classify_frame(frame) != "data":
                    continue
                changed = False
                for chunk in frame.get("data", []):
                    svc = chunk.get("service") or _SERVICE
                    for content in chunk.get("content", []):
                        if _apply_decoded(state, decode(svc, content)):
                            changed = True
                if changed:
                    last_update = datetime.now(tz=_NY)
                    live.update(_build_table(
                        list(state.values()), asof=last_update, live=True,
                    ))
    finally:
        try:
            await streamer.unsubscribe(
                service=_SERVICE, keys=[s.upper() for s in symbols],
            )
        finally:
            await streamer.close()


# ---- quote model -------------------------------------------------------


@dataclass
class QuoteSnapshot:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    last: float | None = None
    volume: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    net_change: float | None = None
    net_change_pct: float | None = None
    _raw: dict[str, Any] = field(default_factory=dict)

    def merged_with(self, decoded: dict[str, Any]) -> "QuoteSnapshot":
        """Streamer frames carry only changed fields. Merge over the
        previous snapshot so unchanged fields keep their values."""
        merged = QuoteSnapshot(symbol=self.symbol)
        for f in (
            "bid", "ask", "bid_size", "ask_size", "last", "volume",
            "open", "high", "low", "close", "net_change", "net_change_pct",
        ):
            merged.__setattr__(
                f, _coerce(decoded.get(f), getattr(self, f)),
            )
        return merged

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid, "ask": self.ask,
            "bid_size": self.bid_size, "ask_size": self.ask_size,
            "last": self.last, "volume": self.volume,
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "net_change": self.net_change,
            "net_change_pct": self.net_change_pct,
        }


def _coerce(new: Any, prev: Any) -> Any:
    """Streamer pushes only fields that changed — preserve previous
    values when a frame omits a field. Treat empty strings the same
    as missing."""
    if new is None or new == "":
        return prev
    return new


_QUOTE_FIELD_MAP = {
    "bid": ("bidPrice", "bid"),
    "ask": ("askPrice", "ask"),
    "bid_size": ("bidSize",),
    "ask_size": ("askSize",),
    "last": ("lastPrice", "last"),
    "volume": ("totalVolume", "volume"),
    "open": ("openPrice", "open"),
    "high": ("highPrice", "high"),
    "low": ("lowPrice", "low"),
    "close": ("closePrice", "close"),
    "net_change": ("netChange",),
    "net_change_pct": ("netPercentChange", "netPercentChangeInDouble"),
}


def _extract_quote(symbol: str, raw: Any) -> QuoteSnapshot:
    """Pull the standard fields out of one Schwab /quotes entry. The
    payload nests under ``quote`` for equity types."""
    if not isinstance(raw, dict):
        return QuoteSnapshot(symbol=symbol)
    quote = raw.get("quote") or raw
    out = QuoteSnapshot(symbol=symbol)
    for attr, keys in _QUOTE_FIELD_MAP.items():
        for k in keys:
            if k in quote and quote[k] is not None:
                setattr(out, attr, quote[k])
                break
    return out


# ---- rendering ---------------------------------------------------------


def _render_table(
    snapshots: list[QuoteSnapshot], *, asof: datetime, live: bool,
) -> None:
    from rich.console import Console
    Console(width=140, soft_wrap=False).print(
        _build_table(snapshots, asof=asof, live=live)
    )


def _build_table(
    snapshots: list[QuoteSnapshot], *, asof: datetime, live: bool,
):
    from rich.table import Table

    title = "Watchlist"
    title += " (live)" if live else ""
    title += f" — last update {asof.strftime('%H:%M:%S')} ET"

    table = Table(title=title, title_justify="left", padding=(0, 1))
    table.add_column("Sym", no_wrap=True, style="bold cyan")
    table.add_column("Bid",  justify="right")
    table.add_column("Ask",  justify="right")
    table.add_column("B.Sz", justify="right", style="dim")
    table.add_column("A.Sz", justify="right", style="dim")
    table.add_column("Last", justify="right")
    table.add_column("Chg",  justify="right")
    table.add_column("Chg%", justify="right")
    table.add_column("Vol",  justify="right")
    table.add_column("Open", justify="right", style="dim")
    table.add_column("High", justify="right", style="dim")
    table.add_column("Low",  justify="right", style="dim")
    table.add_column("PrevC", justify="right", style="dim")

    for s in snapshots:
        chg_style = _chg_style(s.net_change)
        table.add_row(
            s.symbol,
            _money(s.bid), _money(s.ask),
            _size(s.bid_size), _size(s.ask_size),
            _money(s.last),
            f"[{chg_style}]{_money(s.net_change)}[/{chg_style}]",
            f"[{chg_style}]{_pct(s.net_change_pct)}[/{chg_style}]",
            _volume(s.volume),
            _money(s.open), _money(s.high),
            _money(s.low),  _money(s.close),
        )
    return table


def _chg_style(v: Any) -> str:
    if v is None:
        return "dim"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "dim"
    if x > 0:
        return "green"
    if x < 0:
        return "red"
    return "white"


def _money(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _size(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _volume(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


# ---- helpers -----------------------------------------------------------


def _normalize(sym: str) -> str:
    return (sym or "").strip().upper()


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho("No config. Run `schwab setup` first.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho("No session. Run `schwab auth` first.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)
