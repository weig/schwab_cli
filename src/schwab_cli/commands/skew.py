"""`skew` command — option skew / smile metrics at three zoom levels.

Modes, selected by flag:

* **L1** (default): ``schwab_cli skew SYM YYMMDD`` — one chain, one
  static skew table (25Δ / 10Δ RR + butterfly, ATM slope, IV range).
* **L2** ``--term``: ``schwab_cli skew SYM --term YY1 YY2 …`` — skew
  across a user-chosen list of expiries for the same symbol.
* **L2** ``--dtes``: ``schwab_cli skew SYM --dtes 7 30 90`` — skew at
  the expiries closest to each target DTE (needs one discovery fetch
  plus one fetch per picked expiry).
* **L3** ``--cross``: ``schwab_cli skew --cross YYMMDD SYM1 SYM2 …`` —
  cross-ticker skew at a shared expiry.

The analytics layer (:mod:`schwab_cli.analytics.skew`) is pure — this
module only handles argument parsing, API orchestration, and format
dispatch. All per-mode errors (invalid YYMMDD, bad symbols, no
contracts) exit with code 2 for user errors, code 1 for runtime /
network failures.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.skew import (
    compare_across_tickers,
    compute_skew,
    compute_term_structure,
)
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.option_spec import OptionSpecError, parse_option_spec
from schwab_cli.output.chains import shape_envelope
from schwab_cli.output.format import Format, FormatError, pick_format
from schwab_cli.output.skew import render_cross, render_skew, render_term
from schwab_cli.session import load as load_session


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


def _parse_yymmdd(s: str) -> date:
    """YYMMDD → date. Raises :class:`typer.Exit` with a readable message
    on bad grammar, bad dates, or past expiries — keeps call sites from
    repeating the try/except block.
    """
    try:
        return parse_option_spec(s).expiry
    except OptionSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        code = 2 if getattr(e, "kind", "invalid") == "invalid" else 1
        raise typer.Exit(code=code)


def _fetch_single_chain(
    client: SchwabClient,
    symbol: str,
    expiry: date,
    strikes: int,
) -> dict[str, Any]:
    """Fetch the chain for one (symbol, expiry) and shape it into the
    envelope :func:`compute_skew` consumes. Schwab-API failures propagate
    as :class:`typer.Exit`; callers that want to downgrade to a warning
    (e.g. in L2/L3 where one failure shouldn't sink the whole report)
    should catch upstream and swallow instead."""
    try:
        raw = get_chain(
            client,
            symbol.upper(),
            contract_type="ALL",
            strike_count=strikes,
            from_date=expiry,
            to_date=expiry,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(
            f"chain fetch failed for {symbol} {expiry.isoformat()}: {msg}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    envelope = shape_envelope(raw)
    if not envelope.get("contracts"):
        typer.secho(
            f"No contracts for {symbol.upper()} on {expiry.isoformat()}. "
            "Verify the expiry exists and has trading activity.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return envelope


def _fetch_for_report(
    client: SchwabClient,
    symbol: str,
    expiry: date,
    strikes: int,
) -> dict[str, Any] | None:
    """Like :func:`_fetch_single_chain` but converts failures into a
    stderr warning + ``None``. Used by L2 / L3 where we want to render
    whatever chains succeeded instead of bailing on the first error.
    """
    try:
        raw = get_chain(
            client,
            symbol.upper(),
            contract_type="ALL",
            strike_count=strikes,
            from_date=expiry,
            to_date=expiry,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(
            f"[warn] skip {symbol.upper()} {expiry.isoformat()}: {msg}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None
    envelope = shape_envelope(raw)
    if not envelope.get("contracts"):
        typer.secho(
            f"[warn] no contracts for {symbol.upper()} {expiry.isoformat()}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None
    return envelope


# ---- mode: L1 (single chain) ------------------------------------------


def _run_l1(
    symbol: str,
    expiry_str: str,
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiry = _parse_yymmdd(expiry_str)
    client = _client()
    envelope = _fetch_single_chain(client, symbol, expiry, strikes)
    metrics = compute_skew(envelope)
    typer.echo(render_skew(metrics, fmt=fmt), nl=False)


# ---- mode: L2 --term (explicit expiry list) ---------------------------


def _run_term(
    symbol: str,
    expiry_strs: list[str],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiries = [_parse_yymmdd(s) for s in expiry_strs]
    client = _client()
    envelopes: list[dict[str, Any]] = []
    for exp in expiries:
        env = _fetch_for_report(client, symbol, exp, strikes)
        if env is not None:
            envelopes.append(env)
    if not envelopes:
        typer.secho(
            f"No usable chains for {symbol.upper()} across {len(expiries)} expiries.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    metrics = compute_term_structure(envelopes)
    typer.echo(render_term(metrics, fmt=fmt, symbol=symbol.upper()), nl=False)


# ---- mode: L2 --dtes (target DTEs → pick closest expiries) ------------


def _discover_expiries(
    client: SchwabClient,
    symbol: str,
    *,
    max_dte: int,
) -> list[tuple[date, int]]:
    """Return ``(expiry_date, dte)`` pairs available for ``symbol`` up
    to ``max_dte`` days out. Cheap discovery fetch — ``strike_count=2``
    is the minimum Schwab allows while still populating the
    ``callExpDateMap`` keys we need. Failures bubble up as
    :class:`typer.Exit`.
    """
    today = date.today()
    try:
        raw = get_chain(
            client,
            symbol.upper(),
            contract_type="ALL",
            strike_count=2,
            from_date=today,
            to_date=today + timedelta(days=max_dte + 30),
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(
            f"chain discovery failed for {symbol.upper()}: {msg}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    found: set[tuple[date, int]] = set()
    for map_key in ("callExpDateMap", "putExpDateMap"):
        for exp_key in (raw.get(map_key) or {}).keys():
            # Schwab encodes these as "YYYY-MM-DD:DTE".
            exp_part, _, dte_part = exp_key.partition(":")
            try:
                exp_date = date.fromisoformat(exp_part)
                dte = int(dte_part)
            except (ValueError, TypeError):
                continue
            found.add((exp_date, dte))
    return sorted(found, key=lambda pair: pair[1])


def _run_dtes(
    symbol: str,
    target_dtes: list[int],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    client = _client()
    available = _discover_expiries(client, symbol, max_dte=max(target_dtes))
    if not available:
        typer.secho(
            f"No expiries discoverable for {symbol.upper()} within {max(target_dtes)} DTE.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # For each target DTE, pick the closest available expiry. De-dup
    # so that --dtes 30 35 doesn't fetch the same chain twice when the
    # two targets collapse onto the same weekly.
    picked: list[tuple[date, int]] = []
    seen: set[date] = set()
    for target in target_dtes:
        exp, dte = min(available, key=lambda pair: abs(pair[1] - target))
        if exp in seen:
            continue
        seen.add(exp)
        picked.append((exp, dte))

    envelopes: list[dict[str, Any]] = []
    for exp, _dte in picked:
        env = _fetch_for_report(client, symbol, exp, strikes)
        if env is not None:
            envelopes.append(env)
    if not envelopes:
        typer.secho(
            f"No usable chains for {symbol.upper()} at target DTEs {target_dtes}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    metrics = compute_term_structure(envelopes)
    typer.echo(render_term(metrics, fmt=fmt, symbol=symbol.upper()), nl=False)


# ---- mode: L3 --cross -------------------------------------------------


def _run_cross(
    expiry_str: str,
    symbols: list[str],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiry = _parse_yymmdd(expiry_str)
    client = _client()
    envelopes: list[dict[str, Any]] = []
    for sym in symbols:
        env = _fetch_for_report(client, sym, expiry, strikes)
        if env is not None:
            envelopes.append(env)
    if not envelopes:
        typer.secho(
            f"No usable chains across {len(symbols)} symbols at {expiry.isoformat()}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    metrics = compare_across_tickers(envelopes)
    typer.echo(render_cross(metrics, fmt=fmt), nl=False)


# ---- mode: L3 --cross + --dtes (cross-ticker at target DTE) -----------


def _run_cross_dtes(
    target_dte: int,
    symbols: list[str],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    """Compare symbols at the same *target* DTE rather than the same
    calendar date. Each symbol independently picks its closest-available
    expiry — they often won't line up exactly (weekly vs monthly listing
    cycles), which is why the rendered ``DTE`` column is per-row rather
    than a shared header. Cost is 2N API calls (discovery + fetch per
    symbol); for the common case of 3-4 symbols this is acceptable.
    """
    client = _client()
    envelopes: list[dict[str, Any]] = []
    for sym in symbols:
        available = _discover_expiries(client, sym, max_dte=target_dte + 30)
        if not available:
            typer.secho(
                f"[warn] no expiries discoverable for {sym.upper()} within "
                f"{target_dte + 30} DTE",
                fg=typer.colors.YELLOW,
                err=True,
            )
            continue
        exp, _dte = min(available, key=lambda pair: abs(pair[1] - target_dte))
        env = _fetch_for_report(client, sym, exp, strikes)
        if env is not None:
            envelopes.append(env)
    if not envelopes:
        typer.secho(
            f"No usable chains across {len(symbols)} symbols at ~{target_dte} DTE.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    metrics = compare_across_tickers(envelopes)
    typer.echo(render_cross(metrics, fmt=fmt), nl=False)


# ---- entry point ------------------------------------------------------


def run(
    args: list[str],
    *,
    term: bool,
    dtes: bool,
    cross: bool,
    strikes: int,
    as_json: bool,
    as_md: bool,
) -> None:
    """Dispatch the appropriate mode based on flags and positional args.

    Flag precedence:
      1. ``--cross --dtes`` → L3 at target DTE: args[0]=int, args[1:]=symbols.
      2. ``--cross`` → L3 at fixed expiry: args[0]=YYMMDD, args[1:]=symbols.
      3. ``--term`` → L2 explicit expiries: args[0]=symbol, args[1:]=YYMMDDs.
      4. ``--dtes`` → L2 target DTEs: args[0]=symbol, args[1:]=ints.
      5. (default)  → L1, ``args == [symbol, YYMMDD]``.

    ``--term`` is mutually exclusive with both ``--cross`` and ``--dtes``
    — the first because they disagree on what ``args[0]`` means, the
    second because they're both L2 strategies. ``--cross --dtes`` is the
    only legal combination and unlocks "compare these symbols at ~N
    DTE, each symbol picking its own closest expiry".
    """
    if term and cross:
        typer.secho(
            "--term and --cross are mutually exclusive (args[0] means different things).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if term and dtes:
        typer.secho(
            "--term and --dtes are mutually exclusive (both are L2 modes).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if cross and dtes:
        if len(args) < 2:
            typer.secho(
                "Usage: schwab_cli skew --cross --dtes N SYMBOL [SYMBOL ...]",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            target_dte = int(args[0])
        except ValueError:
            typer.secho(
                f"--cross --dtes expects an integer DTE first, got {args[0]!r}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        if target_dte <= 0:
            typer.secho(
                "--cross --dtes expects a positive integer DTE.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        symbols = args[1:]
        _run_cross_dtes(target_dte, symbols, strikes=strikes, fmt=fmt)
        return

    if cross:
        if len(args) < 2:
            typer.secho(
                "Usage: schwab_cli skew --cross YYMMDD SYMBOL [SYMBOL ...]",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        expiry_str, *symbols = args
        _run_cross(expiry_str, symbols, strikes=strikes, fmt=fmt)
        return

    if term:
        if len(args) < 2:
            typer.secho(
                "Usage: schwab_cli skew SYMBOL --term YYMMDD [YYMMDD ...]",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        symbol, *expiry_strs = args
        _run_term(symbol, expiry_strs, strikes=strikes, fmt=fmt)
        return

    if dtes:
        if len(args) < 2:
            typer.secho(
                "Usage: schwab_cli skew SYMBOL --dtes N [N ...]",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        symbol, *dte_strs = args
        try:
            target_dtes = [int(s) for s in dte_strs]
        except ValueError:
            typer.secho(
                f"--dtes values must be integers, got {dte_strs!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        if any(d <= 0 for d in target_dtes):
            typer.secho(
                "--dtes values must be positive integers.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        _run_dtes(symbol, target_dtes, strikes=strikes, fmt=fmt)
        return

    # Default: L1.
    if len(args) != 2:
        typer.secho(
            "Usage: schwab_cli skew SYMBOL YYMMDD "
            "(or use --term / --dtes / --cross for multi-chain modes).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    symbol, expiry_str = args
    _run_l1(symbol, expiry_str, strikes=strikes, fmt=fmt)
