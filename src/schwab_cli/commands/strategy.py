"""`strategy` command — option-strategy probability + risk analysis.

End-to-end wiring of:

1. Leg parsing (:mod:`schwab_cli.analytics.strategy_legs`).
2. Classifier (:mod:`schwab_cli.analytics.strategy_classify`).
3. Chain fetch per unique expiry; per-leg enrichment with premium / IV
   / greeks.
4. Analytics (:mod:`schwab_cli.analytics.strategy`) — skipped for
   out-of-scope shapes (Phase-2 multi-expiry), which still render the
   Schwab ticket and legs table.
5. Ticket rendering (:mod:`schwab_cli.analytics.strategy_ticket`).
6. Output dispatch via :mod:`schwab_cli.output.strategy`.

Exit codes follow the project convention: 0 success, 2 usage error, 1
runtime/network failure.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import typer

from schwab_cli import config as config_module
from schwab_cli.analytics.strategy import (
    PricedLeg,
    breakevens,
    combined_greeks,
    ev,
    max_loss,
    max_profit,
    pop,
    prob_touch,
)
from schwab_cli.analytics.strategy_classify import classify
from schwab_cli.analytics.strategy_legs import Leg, LegParseError, parse_leg
from schwab_cli.analytics.strategy_ticket import render_ticket
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.output.chains import shape_envelope
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.strategy import render_strategy
from schwab_cli.session import load as load_session


# Nearest-strike snap tolerance — deviations bigger than this push a
# warning into the envelope (and stderr) so the user knows the leg
# they typed isn't exactly what got priced.
_SNAP_TOLERANCE = 0.50

# Short-DTE warning threshold — log-normal fit degrades at 0-2 DTE.
_SHORT_DTE_WARN = 3

# Leg IV anomaly threshold — leg IV more than this multiple of the
# shortest-expiry ATM IV triggers a warning.
_IV_ANOMALY_MULTIPLE = 2.0


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


# ---- entry point ------------------------------------------------------


def run(
    symbol: str,
    legs_raw: list[str],
    *,
    risk_free: float,
    as_json: bool,
    as_md: bool,
) -> None:
    """Entry point from :mod:`cli`."""
    # Format + legs are parsed before any network I/O so usage errors
    # exit 2 cleanly without attempting a Schwab fetch.
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if not legs_raw:
        typer.secho(
            "strategy requires at least one --leg. "
            "Example: schwab_cli strategy AMZN --leg +1@20260501C255",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        legs = [parse_leg(tok) for tok in legs_raw]
    except LegParseError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Fetch chains for each unique expiry and enrich legs with live
    # premium / IV / greeks. This is the only I/O path in the command.
    client = _client()
    warnings: list[str] = []
    chains = _fetch_chains(client, symbol, legs)

    priced, spot, shared_dte = _price_legs(legs, chains, warnings)

    # Classify. This is pure — runs even when analytics won't.
    cls = classify(legs)

    # Always render the Schwab ticket, even for unsupported shapes.
    ticket = render_ticket(priced, cls, symbol=symbol)

    # Conditionally compute analytics.
    analytics = _compute_analytics(priced, cls, spot, shared_dte, risk_free, warnings)

    # Add shape-level warnings.
    if cls.naked:
        # Split naked flag into specific warnings so consumers can filter.
        if _has_naked_short(priced, "C"):
            warnings.append("naked_short_call")
        if _has_naked_short(priced, "P"):
            warnings.append("naked_short_put")
    if analytics.get("max_loss") is None and cls.supported:
        warnings.append("unlimited_loss")
    if shared_dte is not None and 0 < shared_dte < _SHORT_DTE_WARN:
        warnings.append(f"short_dte:{shared_dte}d")
    if cls.supported and len(analytics.get("breakevens") or []) >= 2:
        warnings.append("prob_touch_approx")
    if not cls.supported and cls.reason:
        warnings.append(f"analytics_not_supported_yet:{cls.reason}")

    # Net premium convention: signed, positive = credit.
    net_premium = -sum(leg.qty * leg.premium for leg in priced)
    envelope: dict[str, Any] = {
        "symbol": symbol.upper(),
        "strategy": cls.strategy,
        "ticket_name": cls.ticket_name,
        "supported": cls.supported,
        "reason": cls.reason,
        "naked": cls.naked,
        "model": "lognormal_flat_iv" if cls.supported else None,
        "spot": spot,
        "dte": shared_dte,
        "legs": [_leg_dict(leg) for leg in priced],
        "ticket": ticket,
        "net_premium": round(net_premium, 4),
        "net_credit": round(net_premium, 4) if net_premium > 0 else 0.0,
        "net_debit": round(-net_premium, 4) if net_premium < 0 else 0.0,
        **analytics,
        "warnings": warnings,
    }

    typer.echo(render_strategy(envelope, fmt=fmt), nl=False)


# ---- chain fetch + per-leg pricing ------------------------------------


def _fetch_chains(
    client: SchwabClient, symbol: str, legs: list[Leg]
) -> dict[date, dict[str, Any]]:
    """Return ``{expiry: envelope}`` for each unique expiry in ``legs``.

    Failures bail the command with exit code 1 — partial results
    would silently hide legs.
    """
    expiries = sorted({leg.expiry for leg in legs})
    out: dict[date, dict[str, Any]] = {}
    for exp in expiries:
        try:
            raw = get_chain(
                client,
                symbol.upper(),
                contract_type="ALL",
                strike_count=50,
                from_date=exp,
                to_date=exp,
            )
        except (ApiError, SessionExpired) as e:
            msg = str(e) if str(e) else type(e).__name__
            typer.secho(
                f"chain fetch failed for {symbol.upper()} {exp.isoformat()}: {msg}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        env = shape_envelope(raw)
        if not env.get("contracts"):
            typer.secho(
                f"No contracts for {symbol.upper()} on {exp.isoformat()}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        out[exp] = env
    return out


def _price_legs(
    legs: list[Leg],
    chains: dict[date, dict[str, Any]],
    warnings: list[str],
) -> tuple[list[PricedLeg], float | None, int | None]:
    """Enrich each :class:`Leg` with premium + IV + greeks from its
    matching contract. Returns ``(priced_legs, spot, shared_dte)``.

    ``spot`` is taken from the first chain's underlying (all chains on
    the same symbol share spot). ``shared_dte`` is set only for
    single-expiry strategies — multi-expiry returns ``None`` and relies
    on per-leg DTE inside the chain envelope for downstream hooks.
    """
    spot: float | None = None
    dtes: set[int] = set()
    priced: list[PricedLeg] = []
    atm_iv = _anchor_atm_iv(chains)

    for leg in legs:
        env = chains[leg.expiry]
        contract = _snap_contract(env, leg, warnings)

        # spot from first-available underlying; all chains share symbol.
        if spot is None:
            spot = (env.get("underlying") or {}).get("last")

        # Collect DTE for the shared-DTE decision. Chain envelope's dte
        # is set from the first contract; pull it directly.
        dte = env.get("dte")
        if isinstance(dte, int):
            dtes.add(dte)

        iv = contract.get("iv")  # decimal already
        if (
            atm_iv is not None
            and iv is not None
            and iv > atm_iv * _IV_ANOMALY_MULTIPLE
        ):
            label = f"{'C' if leg.side == 'C' else 'P'}{leg.strike:g}"
            warnings.append(f"iv_anomaly:leg_{label}_iv_{iv:.2f}")

        premium = _pick_premium(contract)

        priced.append(
            PricedLeg(
                qty=leg.qty,
                side=leg.side,
                expiry=leg.expiry,
                strike=contract.get("strike") or leg.strike,
                premium=premium,
                iv=iv,
                delta=contract.get("delta"),
                gamma=contract.get("gamma"),
                theta=contract.get("theta"),
                vega=contract.get("vega"),
            )
        )

    shared_dte = next(iter(dtes)) if len(dtes) == 1 else None
    return priced, spot, shared_dte


def _snap_contract(
    env: dict[str, Any], leg: Leg, warnings: list[str]
) -> dict[str, Any]:
    """Return the chain contract with the closest strike on the leg's
    side. Pushes a ``strike_snap:…`` warning when the picked strike
    differs from the requested strike by more than ``_SNAP_TOLERANCE``.
    """
    candidates = [
        c for c in (env.get("contracts") or [])
        if c.get("side") == leg.side and c.get("strike") is not None
    ]
    if not candidates:
        typer.secho(
            f"No {leg.side} contracts available for "
            f"{leg.expiry.isoformat()}; cannot price leg at {leg.strike}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    picked = min(candidates, key=lambda c: abs(c["strike"] - leg.strike))
    if abs(picked["strike"] - leg.strike) > _SNAP_TOLERANCE:
        warnings.append(
            f"strike_snap:{leg.side}{leg.strike:g}→{leg.side}{picked['strike']:g}"
        )
    return picked


def _pick_premium(contract: dict[str, Any]) -> float:
    """Order of preference: mark, mid(bid,ask), last, 0.0. Prevents
    NaN propagation for thin contracts where only one field is set.
    """
    mark = contract.get("mark")
    if isinstance(mark, (int, float)) and mark > 0:
        return float(mark)
    bid = contract.get("bid")
    ask = contract.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid >= 0 and ask > 0:
        return (bid + ask) / 2.0
    last = contract.get("last")
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    return 0.0


def _anchor_atm_iv(chains: dict[date, dict[str, Any]]) -> float | None:
    """Pull an ATM IV reference from the first chain's contracts — the
    call whose delta is closest to 0.50.
    """
    for env in chains.values():
        calls = [
            c for c in (env.get("contracts") or [])
            if c.get("side") == "C" and c.get("delta") is not None
            and c.get("iv") is not None
        ]
        if not calls:
            continue
        atm = min(calls, key=lambda c: abs(abs(c["delta"]) - 0.50))
        return atm.get("iv")
    return None


def _has_naked_short(priced: list[PricedLeg], side: str) -> bool:
    shorts = sum(-L.qty for L in priced if L.side == side and L.qty < 0)
    longs = sum(L.qty for L in priced if L.side == side and L.qty > 0)
    return shorts > longs


# ---- analytics wiring --------------------------------------------------


def _compute_analytics(
    priced: list[PricedLeg],
    cls: Any,
    spot: float | None,
    shared_dte: int | None,
    r: float,
    warnings: list[str],
) -> dict[str, Any]:
    """Run analytics for supported shapes; fill with ``None`` otherwise.

    Always emits the full set of metric keys so the renderer sees a
    stable envelope shape whether or not analytics ran.
    """
    nulls: dict[str, Any] = {
        "pop": None,
        "ev": None,
        "max_profit": None,
        "max_loss": None,
        "breakevens": None,
        "prob_touch": None,
        "greeks": {"delta": None, "gamma": None, "theta": None, "vega": None},
    }
    if not cls.supported:
        return nulls
    if spot is None or shared_dte is None:
        # Safety net — classifier said supported but we somehow can't
        # anchor the density. Downgrade to unsupported rather than
        # compute on garbage inputs.
        warnings.append("analytics_skipped:missing_spot_or_dte")
        return nulls

    bes = breakevens(priced)
    touch_probs = [
        prob_touch(K=be, spot=spot, iv=_avg_iv(priced), dte=shared_dte, r=r)
        for be in bes
    ] if bes else []

    mp = max_profit(priced)
    ml = max_loss(priced)
    greeks = combined_greeks(priced)
    return {
        "pop": round(pop(priced, spot=spot, dte=shared_dte, r=r), 4),
        "ev": round(ev(priced, spot=spot, dte=shared_dte, r=r), 2),
        "max_profit": round(mp, 2) if mp is not None else None,
        "max_loss": round(ml, 2) if ml is not None else None,
        "breakevens": [round(b, 2) for b in bes],
        "prob_touch": [round(p, 4) for p in touch_probs],
        "greeks": {
            k: (round(v, 4) if isinstance(v, (int, float)) else v)
            for k, v in greeks.items()
        },
    }


def _avg_iv(priced: list[PricedLeg]) -> float:
    ivs = [L.iv for L in priced if L.iv is not None]
    return sum(ivs) / len(ivs) if ivs else 0.0


def _leg_dict(leg: PricedLeg) -> dict[str, Any]:
    iv_pct = leg.iv * 100 if leg.iv is not None else None
    return {
        "qty": leg.qty,
        "side": leg.side,
        "strike": leg.strike,
        "expiry": leg.expiry.isoformat(),
        "premium": round(leg.premium, 4),
        "iv_pct": round(iv_pct, 2) if iv_pct is not None else None,
        "delta": leg.delta,
        "gamma": leg.gamma,
        "theta": leg.theta,
        "vega": leg.vega,
    }
