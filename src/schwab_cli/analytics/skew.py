"""Skew / smile analytics — pure math, no I/O.

Given a single option-chain envelope (one symbol, one expiry, flat
contract list with ``side``/``strike``/``delta``/``iv``), computes the
standard skew metrics: 25Δ and 10Δ risk reversals, butterflies, ATM IV,
ATM slope, and IV range.

The three public entry points map 1:1 to the three ``skew`` command
modes:

* :func:`compute_skew` — L1 static skew for a single chain.
* :func:`compute_term_structure` — L2: a list of L1 results sorted by
  DTE ascending.
* :func:`compare_across_tickers` — L3: a list of L1 results across
  different symbols at the same (or similar) DTE, sorted by 25Δ RR
  descending (largest put premium first).

All functions are deterministic and have no external dependencies. IVs
in the input are decimal (``0.6162`` = 61.62%); all IV-derived outputs
carry an ``_pct`` suffix and are in vol points (0-100 scale) — the
industry convention for skew reporting.

Robustness rules:

* A contract missing ``delta`` or ``iv`` is **skipped** (not an error).
* An empty or wing-missing chain returns a metrics dict with ``None``
  values — the renderer shows ``—`` rather than raising.
* Missing top-level keys (``underlying`` / ``contracts``) raise
  :class:`ValueError` — the contract is broken, not just thin.
"""

from __future__ import annotations

from typing import Any

# Half-width of the near-ATM window used by :func:`_atm_slope`. Strikes
# whose distance from spot is within ±``_SLOPE_WINDOW`` are included in
# the pairwise slope average. $15 covers three or more strikes on
# liquid US equities at typical prices; tight enough to avoid diluting
# the near-ATM signal with the wings.
_SLOPE_WINDOW = 15.0

# Minimum strikes inside the near-ATM window required to compute a
# slope. Two strikes would give a single pair — too noisy to publish.
_SLOPE_MIN_STRIKES = 3


def _find_by_delta(
    contracts: list[dict[str, Any]],
    target_abs_delta: float,
) -> dict[str, Any] | None:
    """Pick the contract whose ``|delta|`` is closest to ``target_abs_delta``.

    Contracts lacking a usable delta are skipped. Returns ``None`` when
    no contract carries a delta.
    """
    valid = [c for c in contracts if c.get("delta") is not None]
    if not valid:
        return None
    return min(valid, key=lambda c: abs(abs(c["delta"]) - target_abs_delta))


def _atm_slope(
    calls_sorted: list[dict[str, Any]],
    spot: float,
    *,
    window: float = _SLOPE_WINDOW,
) -> float | None:
    """Average ``dIV/dStrike`` across consecutive strike pairs near ATM.

    The output is in **vol points per $1 of strike**. A negative value
    is the canonical put skew (higher-strike calls carry lower IV); a
    positive value is call skew, rare outside deep ITM or LEAPS.
    """
    near = [
        c for c in calls_sorted
        if c.get("strike") is not None
        and c.get("iv") is not None
        and abs(c["strike"] - spot) <= window
    ]
    if len(near) < _SLOPE_MIN_STRIKES:
        return None
    slopes: list[float] = []
    for a, b in zip(near, near[1:]):
        ds = b["strike"] - a["strike"]
        if ds > 0:
            slopes.append((b["iv"] - a["iv"]) * 100 / ds)
    return sum(slopes) / len(slopes) if slopes else None


def _fmt_leg(c: dict[str, Any] | None) -> dict[str, Any] | None:
    """Flatten a contract dict into the ``{strike, delta, iv_pct}`` leg
    shape the renderer consumes. Returns ``None`` when the input is
    ``None`` so callers can chain through."""
    if not c:
        return None
    iv = c.get("iv")
    return {
        "strike": c.get("strike"),
        "delta": c.get("delta"),
        "iv_pct": iv * 100 if iv is not None else None,
    }


def _rr(p: dict | None, c: dict | None) -> float | None:
    """25Δ / 10Δ risk reversal = (put IV - call IV) × 100 vol pt."""
    if not (p and c):
        return None
    if p.get("iv") is None or c.get("iv") is None:
        return None
    return (p["iv"] - c["iv"]) * 100


def _bf(p: dict | None, c: dict | None, atm_iv: float | None) -> float | None:
    """Butterfly = ((put IV + call IV) / 2 - ATM IV) × 100 vol pt.

    Positive → wings higher than ATM (convex smile). Negative → wings
    below ATM (inverted, rare).
    """
    if not (p and c) or atm_iv is None:
        return None
    if p.get("iv") is None or c.get("iv") is None:
        return None
    return ((p["iv"] + c["iv"]) / 2 - atm_iv) * 100


def compute_skew(chain: dict[str, Any]) -> dict[str, Any]:
    """Compute L1 skew metrics from a single option chain.

    Args:
        chain: Envelope with keys ``symbol``, ``expiry``, ``dte``,
            ``underlying.last``, and ``contracts`` — each contract
            carrying at least ``side`` (``"C"`` / ``"P"``), ``strike``,
            ``delta``, and ``iv`` (decimal, ``0.60`` = 60%).

    Returns:
        Metrics dict per the ``skew`` JSON schema — ``atm``, ``d25``,
        ``d10``, ``atm_slope_per_dollar``, ``iv_range``, plus the
        pass-through ``symbol`` / ``expiry`` / ``dte`` / ``spot``
        context fields.

    Raises:
        ValueError: If ``chain`` lacks ``underlying`` or ``contracts``.
    """
    if "underlying" not in chain or "contracts" not in chain:
        raise ValueError(
            "chain envelope must contain 'underlying' and 'contracts' keys"
        )

    underlying = chain.get("underlying") or {}
    spot = underlying.get("last")

    expiry_raw = chain.get("expiry") or ""
    expiry = expiry_raw[:10] if isinstance(expiry_raw, str) else ""

    all_contracts = chain.get("contracts") or []
    calls = sorted(
        [c for c in all_contracts if c.get("side") == "C" and c.get("strike") is not None],
        key=lambda c: c["strike"],
    )
    puts = sorted(
        [c for c in all_contracts if c.get("side") == "P" and c.get("strike") is not None],
        key=lambda c: c["strike"],
    )

    atm_c = _find_by_delta(calls, 0.50)
    atm_p = _find_by_delta(puts, 0.50)
    # ATM IV comes from the call leg — put-call parity keeps them
    # near-identical, and calls are the leg traders anchor skew
    # calculations on by convention.
    atm_iv = atm_c.get("iv") if atm_c else None

    p25 = _find_by_delta(puts, 0.25)
    c25 = _find_by_delta(calls, 0.25)
    p10 = _find_by_delta(puts, 0.10)
    c10 = _find_by_delta(calls, 0.10)

    call_ivs = [c["iv"] for c in calls if c.get("iv") is not None]
    slope = _atm_slope(calls, spot) if spot is not None else None

    return {
        "symbol": chain.get("symbol") or "",
        "expiry": expiry,
        "dte": chain.get("dte"),
        "spot": spot,
        "atm": {
            "strike": atm_c.get("strike") if atm_c else None,
            "iv_pct": atm_iv * 100 if atm_iv is not None else None,
            "put_strike": atm_p.get("strike") if atm_p else None,
            "put_iv_pct": (
                atm_p["iv"] * 100
                if atm_p and atm_p.get("iv") is not None
                else None
            ),
        },
        "d25": {
            "put": _fmt_leg(p25),
            "call": _fmt_leg(c25),
            "rr": _rr(p25, c25),
            "bf": _bf(p25, c25, atm_iv),
        },
        "d10": {
            "put": _fmt_leg(p10),
            "call": _fmt_leg(c10),
            "rr": _rr(p10, c10),
            "bf": _bf(p10, c10, atm_iv),
        },
        "atm_slope_per_dollar": slope,
        "iv_range": {
            "min_pct": min(call_ivs) * 100 if call_ivs else None,
            "max_pct": max(call_ivs) * 100 if call_ivs else None,
            "spread_pct": (
                (max(call_ivs) - min(call_ivs)) * 100 if call_ivs else None
            ),
        },
    }


def compute_term_structure(chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L2 — per-expiry metrics for one symbol, sorted by DTE ascending.

    Missing DTE sorts to the end (treated as +∞) so a malformed chain
    doesn't hide a well-formed one at the top of the table.
    """
    metrics = [compute_skew(c) for c in chains]
    return sorted(
        metrics,
        key=lambda m: m["dte"] if m.get("dte") is not None else 10**9,
    )


def compare_across_tickers(chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L3 — cross-ticker metrics, sorted by 25Δ RR descending.

    Tickers whose 25Δ RR is ``None`` (typically thin wings) sort last so
    they don't displace a ranked top row.
    """
    metrics = [compute_skew(c) for c in chains]

    def key(m: dict[str, Any]) -> float:
        rr = (m.get("d25") or {}).get("rr")
        # Negate for descending. None → +inf puts it last after sort asc.
        return -rr if rr is not None else float("inf")

    return sorted(metrics, key=key)
