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

This module is a thin Layer-3 shim: it owns argument parsing /
validation and exit-code mapping, then dispatches to the Layer-2
:mod:`schwab_cli.service.skew` (which owns auth + fetch + compute) and
renders via :mod:`schwab_cli.output.skew`. All per-mode argument errors
exit with code 2; auth / runtime / network failures exit with code 1.
"""

from __future__ import annotations

from datetime import date

import typer

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.commands._error import cli_errors
from schwab_cli.commands._output import skew_cli_sink
from schwab_cli.option_spec import OptionSpecError, parse_option_spec
from schwab_cli.output.format import Format, FormatError, pick_format
from schwab_cli.output.skew import render_cross, render_skew, render_term
from schwab_cli.service.skew import SkewService


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


def _fail(message: str, *, code: int) -> None:
    """Print ``message`` to stderr in red and exit with ``code``."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


# Partial-failure skip notices (YELLOW / stderr) are emitted by the service
# through the injected output sink (`skew_cli_sink()`), reproducing the old
# `_warn` callback exactly.
#
# Auth errors (NotConfigured / NotAuthenticated) and the bare-message service
# errors (NoSkewData with `str(e)`, DiscoveryError) are routed through the
# @cli_errors decorator on `run`. Only the ApiError / SessionExpired cases that
# need a *custom* wrapping message (naming the symbol / expiry) stay local.


# ---- mode: L1 (single chain) ------------------------------------------


def _run_l1(
    symbol: str,
    expiry_str: str,
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiry = _parse_yymmdd(expiry_str)
    try:
        result = SkewService(out=skew_cli_sink()).get_skew_l1(
            symbol, expiry, strikes=strikes
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        _fail(
            f"chain fetch failed for {symbol} {expiry.isoformat()}: {msg}",
            code=1,
        )
    typer.echo(render_skew(result.metrics, fmt=fmt), nl=False)


# ---- mode: L2 --term (explicit expiry list) ---------------------------


def _run_term(
    symbol: str,
    expiry_strs: list[str],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiries = [_parse_yymmdd(s) for s in expiry_strs]
    result = SkewService(out=skew_cli_sink()).get_skew_term(
        symbol, expiries, strikes=strikes
    )
    typer.echo(render_term(result.metrics, fmt=fmt, symbol=result.symbol), nl=False)


# ---- mode: L2 --dtes (target DTEs → pick closest expiries) ------------


def _run_dtes(
    symbol: str,
    target_dtes: list[int],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    try:
        result = SkewService(out=skew_cli_sink()).get_skew_dtes(
            symbol, target_dtes, strikes=strikes
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        _fail(f"chain discovery failed for {symbol.upper()}: {msg}", code=1)
    typer.echo(render_term(result.metrics, fmt=fmt, symbol=result.symbol), nl=False)


# ---- mode: L3 --cross -------------------------------------------------


def _run_cross(
    expiry_str: str,
    symbols: list[str],
    *,
    strikes: int,
    fmt: Format,
) -> None:
    expiry = _parse_yymmdd(expiry_str)
    result = SkewService(out=skew_cli_sink()).get_skew_cross(
        expiry, symbols, strikes=strikes
    )
    typer.echo(render_cross(result.metrics, fmt=fmt), nl=False)


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
    than a shared header.
    """
    # NoSkewData, DiscoveryError (both ServiceError, bare `str(e)` + exit 1) and
    # the up-front token-mint ApiError / SessionExpired all map to the canonical
    # `str(e) or type(e).__name__` + exit 1 — identical to @cli_errors — so they
    # are routed through the decorator on `run` rather than handled locally.
    result = SkewService(out=skew_cli_sink()).get_skew_cross_dtes(
        target_dte, symbols, strikes=strikes
    )
    typer.echo(render_cross(result.metrics, fmt=fmt), nl=False)


# ---- entry point ------------------------------------------------------


@cli_errors
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
