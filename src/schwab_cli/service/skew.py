"""Layer-2 service for the ``skew`` command — option skew / smile metrics.

Owns auth + the fetch/compute orchestration that used to live in
``commands/skew.py``: one Schwab request per (symbol, expiry), the
discovery fetch for the ``--dtes`` modes, and the
``compute_skew`` / ``compute_term_structure`` / ``compare_across_tickers``
calls. The command (Layer 3) becomes a thin parse -> service -> render
shim that keeps all argument validation and exit-code mapping.

Modes:

* **L1** (``get_skew_l1``): one chain → a single skew metrics dict.
* **L2 --term** (``get_skew_term``): explicit expiry list → per-expiry
  metrics, partial-failure tolerant.
* **L2 --dtes** (``get_skew_dtes``): target DTEs → discovery fetch picks
  the closest expiries → per-expiry metrics, partial-failure tolerant.
* **L3 --cross** (``get_skew_cross``): shared expiry across symbols →
  per-symbol metrics, partial-failure tolerant.
* **L3 --cross --dtes** (``get_skew_cross_dtes``): per-symbol discovery
  + closest-expiry pick → per-symbol metrics, partial-failure tolerant.

Layer-1 is reached via the MODULE ATTRIBUTE ``api_chains.get_chain`` —
the stable test seam the characterization suite patches.

Partial-failure tolerance is preserved exactly: in the term / cross
modes a per-expiry / per-symbol ``(ApiError, SessionExpired)`` (or an
empty envelope) is skipped via the optional ``on_skip`` callback, and
only when EVERY fetch fails does the service raise :class:`NoSkewData`
(exit 1). The single-chain L1 mode propagates ``(ApiError,
SessionExpired)`` unchanged and raises :class:`NoSkewData` on an empty
envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from schwab_cli import config as config_module
from schwab_cli.analytics.skew import (
    compare_across_tickers,
    compute_skew,
    compute_term_structure,
)
from schwab_cli.api import chains as api_chains
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.output.chains import shape_envelope
from schwab_cli.service import ServiceError
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.types import SkewResult

__all__ = [
    "NoSkewData",
    "get_skew_l1",
    "get_skew_term",
    "get_skew_dtes",
    "get_skew_cross",
    "get_skew_cross_dtes",
]

# Callback invoked with a ready-to-print warning string when a per-expiry
# / per-symbol fetch is skipped in the partial-failure-tolerant modes. The
# command supplies a printer; ``None`` (the default) is silent.
OnSkip = Callable[[str], None]


class NoSkewData(ServiceError):
    """Raised when no usable chain data could be produced for the request.

    Covers both the L1 empty-envelope case and the all-fetches-failed case
    in the term / cross modes. Carries a complete, user-ready message —
    ``str(e)`` is the full sentence, so interfaces can surface it directly.
    """


class DiscoveryError(ServiceError):
    """Raised when expiry discovery fails for a symbol in a multi-symbol mode.

    Carries the symbol so the complete message (``str(e)``) names it, matching
    the single-symbol ``dtes`` path. Discovery failure is fatal for the run.
    """

    def __init__(self, sym: str, cause: Exception) -> None:
        self.sym = sym
        super().__init__(f"chain discovery failed for {sym.upper()}: {cause}")


def _fetch_single_chain(
    client: SchwabClient,
    symbol: str,
    expiry: date,
    strikes: int,
) -> dict[str, Any]:
    """Fetch the chain for one (symbol, expiry) and shape it into the
    envelope :func:`compute_skew` consumes.

    ``(ApiError, SessionExpired)`` propagate unchanged. An empty envelope
    raises :class:`NoSkewData` with the same message the command used to
    print before bailing.
    """
    raw = api_chains.get_chain(
        client,
        symbol.upper(),
        contract_type="ALL",
        strike_count=strikes,
        from_date=expiry,
        to_date=expiry,
    )
    envelope = shape_envelope(raw)
    if not envelope.get("contracts"):
        raise NoSkewData(
            f"No contracts for {symbol.upper()} on {expiry.isoformat()}. "
            "Verify the expiry exists and has trading activity."
        )
    return envelope


def _fetch_for_report(
    client: SchwabClient,
    symbol: str,
    expiry: date,
    strikes: int,
    *,
    on_skip: OnSkip | None,
) -> dict[str, Any] | None:
    """Like :func:`_fetch_single_chain` but converts failures into a
    skip notice + ``None``. Used by the term / cross modes where we want
    to render whatever chains succeeded instead of bailing on the first
    error.
    """
    try:
        raw = api_chains.get_chain(
            client,
            symbol.upper(),
            contract_type="ALL",
            strike_count=strikes,
            from_date=expiry,
            to_date=expiry,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        if on_skip is not None:
            on_skip(f"[warn] skip {symbol.upper()} {expiry.isoformat()}: {msg}")
        return None
    envelope = shape_envelope(raw)
    if not envelope.get("contracts"):
        if on_skip is not None:
            on_skip(f"[warn] no contracts for {symbol.upper()} {expiry.isoformat()}")
        return None
    return envelope


def _discover_expiries(
    client: SchwabClient,
    symbol: str,
    *,
    max_dte: int,
) -> list[tuple[date, int]]:
    """Return ``(expiry_date, dte)`` pairs available for ``symbol`` up to
    ``max_dte`` days out. Cheap discovery fetch — ``strike_count=2`` is the
    minimum Schwab allows while still populating the ``callExpDateMap`` keys
    we need. ``(ApiError, SessionExpired)`` propagate unchanged.
    """
    today = date.today()
    raw = api_chains.get_chain(
        client,
        symbol.upper(),
        contract_type="ALL",
        strike_count=2,
        from_date=today,
        to_date=today + timedelta(days=max_dte + 30),
    )
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


def _load_client_ctx() -> tuple[Any, Any]:
    """Resolve config + session, returning ``(cfg, session)``.

    Raises :class:`NotConfigured` when no config is on disk, and the auth
    exceptions from :mod:`schwab_cli.service.auth` when the session is
    missing / expired.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured
    session = service_auth.get_session(cfg)
    return cfg, session


# ---- mode: L1 (single chain) ------------------------------------------


def get_skew_l1(
    symbol: str,
    expiry: date,
    *,
    strikes: int,
) -> SkewResult:
    """L1: fetch one chain, compute its skew metrics.

    ``(ApiError, SessionExpired)`` propagate unchanged; an empty envelope
    raises :class:`NoSkewData`.
    """
    cfg, session = _load_client_ctx()
    with SchwabClient(cfg, session) as client:
        envelope = _fetch_single_chain(client, symbol, expiry, strikes)
    metrics = compute_skew(envelope)
    return SkewResult(metrics=metrics)


# ---- mode: L2 --term (explicit expiry list) ---------------------------


def get_skew_term(
    symbol: str,
    expiries: list[date],
    *,
    strikes: int,
    on_skip: OnSkip | None = None,
) -> SkewResult:
    """L2 --term: fetch each expiry, compute the term structure.

    Per-expiry fetch failures are skipped (``on_skip``); only when EVERY
    expiry fails does this raise :class:`NoSkewData`.
    """
    cfg, session = _load_client_ctx()
    envelopes: list[dict[str, Any]] = []
    with SchwabClient(cfg, session) as client:
        for exp in expiries:
            env = _fetch_for_report(client, symbol, exp, strikes, on_skip=on_skip)
            if env is not None:
                envelopes.append(env)
    if not envelopes:
        raise NoSkewData(
            f"No usable chains for {symbol.upper()} across {len(expiries)} expiries."
        )
    metrics = compute_term_structure(envelopes)
    return SkewResult(metrics=metrics, symbol=symbol.upper())


# ---- mode: L2 --dtes (target DTEs → pick closest expiries) ------------


def get_skew_dtes(
    symbol: str,
    target_dtes: list[int],
    *,
    strikes: int,
    on_skip: OnSkip | None = None,
) -> SkewResult:
    """L2 --dtes: discover expiries, pick the closest to each target DTE,
    fetch + compute the term structure.

    The discovery fetch propagates ``(ApiError, SessionExpired)``. An empty
    discovery result or all-fetches-failed raises :class:`NoSkewData`.
    Per-expiry fetch failures are skipped (``on_skip``).
    """
    cfg, session = _load_client_ctx()
    with SchwabClient(cfg, session) as client:
        available = _discover_expiries(client, symbol, max_dte=max(target_dtes))
        if not available:
            raise NoSkewData(
                f"No expiries discoverable for {symbol.upper()} "
                f"within {max(target_dtes)} DTE."
            )

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
            env = _fetch_for_report(client, symbol, exp, strikes, on_skip=on_skip)
            if env is not None:
                envelopes.append(env)
    if not envelopes:
        raise NoSkewData(
            f"No usable chains for {symbol.upper()} at target DTEs {target_dtes}."
        )
    metrics = compute_term_structure(envelopes)
    return SkewResult(metrics=metrics, symbol=symbol.upper())


# ---- mode: L3 --cross -------------------------------------------------


def get_skew_cross(
    expiry: date,
    symbols: list[str],
    *,
    strikes: int,
    on_skip: OnSkip | None = None,
) -> SkewResult:
    """L3 --cross: fetch each symbol at a shared expiry, compare them.

    Per-symbol fetch failures are skipped (``on_skip``); only when EVERY
    symbol fails does this raise :class:`NoSkewData`.
    """
    cfg, session = _load_client_ctx()
    envelopes: list[dict[str, Any]] = []
    with SchwabClient(cfg, session) as client:
        for sym in symbols:
            env = _fetch_for_report(client, sym, expiry, strikes, on_skip=on_skip)
            if env is not None:
                envelopes.append(env)
    if not envelopes:
        raise NoSkewData(
            f"No usable chains across {len(symbols)} symbols at {expiry.isoformat()}."
        )
    metrics = compare_across_tickers(envelopes)
    return SkewResult(metrics=metrics)


# ---- mode: L3 --cross + --dtes (cross-ticker at target DTE) -----------


def get_skew_cross_dtes(
    target_dte: int,
    symbols: list[str],
    *,
    strikes: int,
    on_skip: OnSkip | None = None,
) -> SkewResult:
    """L3 --cross --dtes: compare symbols at the same *target* DTE rather
    than the same calendar date. Each symbol independently discovers and
    picks its closest-available expiry. Cost is 2N API calls (discovery +
    fetch per symbol).

    Per-symbol discovery-empty or fetch failures are skipped (``on_skip``);
    only when EVERY symbol yields nothing does this raise
    :class:`NoSkewData`.
    """
    cfg, session = _load_client_ctx()
    envelopes: list[dict[str, Any]] = []
    with SchwabClient(cfg, session) as client:
        for sym in symbols:
            try:
                available = _discover_expiries(client, sym, max_dte=target_dte + 30)
            except (ApiError, SessionExpired) as e:
                raise DiscoveryError(sym, e) from e
            if not available:
                if on_skip is not None:
                    on_skip(
                        f"[warn] no expiries discoverable for {sym.upper()} within "
                        f"{target_dte + 30} DTE"
                    )
                continue
            exp, _dte = min(available, key=lambda pair: abs(pair[1] - target_dte))
            env = _fetch_for_report(client, sym, exp, strikes, on_skip=on_skip)
            if env is not None:
                envelopes.append(env)
    if not envelopes:
        raise NoSkewData(
            f"No usable chains across {len(symbols)} symbols at ~{target_dte} DTE."
        )
    metrics = compare_across_tickers(envelopes)
    return SkewResult(metrics=metrics)
