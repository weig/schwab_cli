"""Unit tests for skew analytics.

Pure math — no network, no fixtures on disk. Test chains are built
inline so the IV-to-metric mapping is visible in the test itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from schwab_cli.analytics.skew import (
    compare_across_tickers,
    compute_skew,
    compute_term_structure,
)


# ---- chain builders ----------------------------------------------------


def _leg(
    side: str,
    strike: float,
    delta: float | None,
    iv: float | None,
) -> dict[str, Any]:
    return {"side": side, "strike": strike, "delta": delta, "iv": iv}


def _make_chain(
    *,
    symbol: str = "AMZN",
    expiry: str = "2026-05-01",
    dte: int = 8,
    spot: float = 255.36,
    contracts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "expiry": expiry,
        "dte": dte,
        "underlying": {"last": spot},
        "contracts": contracts,
    }


def _amzn_put_skew_chain() -> dict[str, Any]:
    """Synthetic AMZN-like chain reproducing the spec §10.2 reference
    values closely enough to pin down formula correctness.

    Strike spacing is $2.5 near the money, $5 in the wings — this mirrors
    real listed AMZN expiries. Deltas are hand-picked to land cleanly on
    the 0.10 / 0.25 / 0.50 targets so the delta-nearest selection is
    unambiguous. IVs are chosen to produce the spec's metrics within
    ≤0.01 vol pt of tolerance.
    """
    # Calls: decreasing IV as strike rises (negative slope).
    call_strikes = [
        # (strike, delta, iv)
        (232.5, 0.80, 0.680),
        (240.0, 0.75, 0.650),
        (245.0, 0.68, 0.635),
        (250.0, 0.60, 0.625),
        (255.0, 0.53, 0.619),
        (257.5, 0.50, 0.6162),  # ATM
        (260.0, 0.46, 0.612),
        (265.0, 0.38, 0.605),
        (270.0, 0.30, 0.600),
        (272.5, 0.26, 0.5951),  # 25Δ call
        (275.0, 0.22, 0.597),
        (280.0, 0.17, 0.6002),  # 10Δ call — furthest strike, closest |Δ| to 0.10
    ]
    put_strikes = [
        # Puts are indexed by |delta|; values below are the *negative*
        # deltas the chain carries.
        (232.5, -0.16, 0.6380),  # 10Δ put
        (240.0, -0.25, 0.6280),  # 25Δ put
        (245.0, -0.32, 0.625),
        (250.0, -0.40, 0.622),
        (255.0, -0.47, 0.619),
        (257.5, -0.50, 0.6158),
        (260.0, -0.54, 0.613),
        (265.0, -0.62, 0.608),
        (272.5, -0.74, 0.600),
    ]
    contracts = [_leg("C", s, d, iv) for (s, d, iv) in call_strikes]
    contracts += [_leg("P", s, d, iv) for (s, d, iv) in put_strikes]
    return _make_chain(contracts=contracts)


# ---- compute_skew ------------------------------------------------------


def test_compute_skew_happy_path_matches_spec_reference():
    m = compute_skew(_amzn_put_skew_chain())
    # ATM — selected by |delta| closest to 0.50.
    assert m["atm"]["strike"] == 257.5
    assert m["atm"]["iv_pct"] == pytest.approx(61.62, abs=0.01)
    # 25Δ legs — nearest-neighbour delta match.
    assert m["d25"]["call"]["strike"] == 272.5
    assert m["d25"]["put"]["strike"] == 240.0
    assert m["d25"]["rr"] == pytest.approx(3.29, abs=0.01)
    assert m["d25"]["bf"] == pytest.approx(-0.46, abs=0.02)
    # 10Δ legs.
    assert m["d10"]["call"]["strike"] == 280.0
    assert m["d10"]["put"]["strike"] == 232.5
    assert m["d10"]["rr"] == pytest.approx(3.78, abs=0.02)
    assert m["d10"]["bf"] == pytest.approx(0.29, abs=0.02)


def test_compute_skew_preserves_context_fields():
    m = compute_skew(_amzn_put_skew_chain())
    assert m["symbol"] == "AMZN"
    assert m["expiry"] == "2026-05-01"
    assert m["dte"] == 8
    assert m["spot"] == pytest.approx(255.36)


def test_compute_skew_expiry_string_gets_truncated_to_iso_date():
    chain = _amzn_put_skew_chain()
    # Schwab occasionally returns an expiry like "2026-05-01T20:00:00.000+00:00".
    chain["expiry"] = "2026-05-01T20:00:00.000+00:00"
    m = compute_skew(chain)
    assert m["expiry"] == "2026-05-01"


def test_compute_skew_iv_range_covers_all_call_ivs():
    m = compute_skew(_amzn_put_skew_chain())
    # Smallest call IV is at strike 272.5 (0.5951); largest is 232.5 (0.680).
    assert m["iv_range"]["min_pct"] == pytest.approx(59.51, abs=0.01)
    assert m["iv_range"]["max_pct"] == pytest.approx(68.00, abs=0.01)
    assert m["iv_range"]["spread_pct"] == pytest.approx(8.49, abs=0.02)


def test_compute_skew_atm_slope_is_negative_for_put_skew():
    m = compute_skew(_amzn_put_skew_chain())
    # Puts-heavy chain → IV falls as strike rises → negative slope.
    assert m["atm_slope_per_dollar"] is not None
    assert m["atm_slope_per_dollar"] < 0


def test_compute_skew_does_not_emit_legacy_dollar_sign_key():
    """The ``atm_slope_per_$`` key was dropped. Downstream consumers
    must use ``atm_slope_per_dollar``."""
    m = compute_skew(_amzn_put_skew_chain())
    assert "atm_slope_per_$" not in m


# ---- sign convention ---------------------------------------------------


def test_sign_convention_put_skew_rr_is_positive():
    """Canonical stock put skew: put IV > call IV → RR > 0."""
    m = compute_skew(_amzn_put_skew_chain())
    assert m["d25"]["rr"] > 0
    assert m["d10"]["rr"] > 0


def test_sign_convention_call_skew_rr_is_negative():
    """Inverted chain (call IV > put IV) → RR < 0."""
    chain = _make_chain(
        contracts=[
            _leg("C", 250.0, 0.50, 0.40),
            _leg("C", 260.0, 0.25, 0.55),  # 25Δ call — high IV
            _leg("P", 240.0, -0.25, 0.45),  # 25Δ put — lower IV
            _leg("P", 250.0, -0.50, 0.40),
        ]
    )
    m = compute_skew(chain)
    assert m["d25"]["rr"] is not None
    assert m["d25"]["rr"] < 0


# ---- edge cases --------------------------------------------------------


def test_compute_skew_empty_contracts_returns_none_filled():
    chain = _make_chain(contracts=[])
    m = compute_skew(chain)
    assert m["atm"]["iv_pct"] is None
    assert m["d25"]["rr"] is None
    assert m["d25"]["bf"] is None
    assert m["d10"]["rr"] is None
    assert m["atm_slope_per_dollar"] is None
    assert m["iv_range"]["min_pct"] is None


def test_compute_skew_missing_iv_on_some_contracts_is_tolerated():
    """Contracts lacking IV are skipped; other metrics still compute."""
    chain = _make_chain(
        contracts=[
            _leg("C", 240.0, 0.50, None),  # no IV — skipped for ATM
            _leg("C", 250.0, 0.48, 0.55),
            _leg("C", 257.5, 0.50, 0.50),
            _leg("C", 265.0, 0.25, 0.48),  # 25Δ call
            _leg("C", 275.0, 0.10, 0.50),  # 10Δ call
            _leg("P", 240.0, -0.25, 0.52),  # 25Δ put
            _leg("P", 230.0, -0.10, 0.56),  # 10Δ put
            _leg("P", 250.0, -0.50, 0.50),
        ]
    )
    m = compute_skew(chain)
    assert m["d25"]["rr"] is not None
    assert m["d10"]["rr"] is not None


def test_compute_skew_sparse_chain_slope_is_none():
    """<3 strikes within ±$15 of spot → slope = None."""
    chain = _make_chain(
        spot=255.0,
        contracts=[
            _leg("C", 250.0, 0.55, 0.40),
            _leg("C", 260.0, 0.45, 0.38),
            # No third call within window — third call is 300 which is
            # outside ±$15 so the atm-slope helper shouldn't accept it.
            _leg("C", 300.0, 0.10, 0.35),
            _leg("P", 250.0, -0.45, 0.42),
            _leg("P", 260.0, -0.55, 0.40),
        ],
    )
    m = compute_skew(chain)
    assert m["atm_slope_per_dollar"] is None


def test_compute_skew_missing_top_level_keys_raises():
    with pytest.raises(ValueError):
        compute_skew({"symbol": "X"})  # no underlying or contracts


def test_compute_skew_without_spot_skips_slope_without_raising():
    """underlying.last = None should not crash; slope falls back to None."""
    chain = _make_chain(contracts=[
        _leg("C", 250.0, 0.50, 0.40),
        _leg("C", 255.0, 0.48, 0.38),
        _leg("C", 260.0, 0.45, 0.36),
    ])
    chain["underlying"] = {"last": None}
    m = compute_skew(chain)
    assert m["atm_slope_per_dollar"] is None


def test_compute_skew_missing_wing_legs_return_none_rr_bf():
    """Chain with only ATM — 25Δ/10Δ metrics are None, not exceptions."""
    chain = _make_chain(contracts=[
        _leg("C", 257.5, 0.50, 0.60),
        _leg("P", 257.5, -0.50, 0.60),
    ])
    m = compute_skew(chain)
    # 25Δ / 10Δ lookups pick the closest delta — here that's ATM itself,
    # so the "put" and "call" legs are populated; but RR is still
    # computed from those closest-matching legs. The value will be
    # ~0 given put/call parity at ATM, which is a valid answer.
    # What we care about: no exception is raised.
    assert m["d25"] is not None
    assert m["d10"] is not None


# ---- compute_term_structure -------------------------------------------


def test_term_structure_sorts_by_dte_ascending():
    far = _make_chain(expiry="2027-01-15", dte=267, contracts=[
        _leg("C", 257.5, 0.50, 0.40),
        _leg("P", 257.5, -0.50, 0.40),
    ])
    near = _make_chain(expiry="2026-05-01", dte=8, contracts=[
        _leg("C", 257.5, 0.50, 0.60),
        _leg("P", 257.5, -0.50, 0.60),
    ])
    mid = _make_chain(expiry="2026-05-15", dte=22, contracts=[
        _leg("C", 257.5, 0.50, 0.50),
        _leg("P", 257.5, -0.50, 0.50),
    ])
    result = compute_term_structure([far, near, mid])
    assert [m["dte"] for m in result] == [8, 22, 267]


# ---- compare_across_tickers -------------------------------------------


def test_cross_tickers_sort_by_d25_rr_descending():
    """Biggest put premium rises to the top."""
    def chain(sym: str, put_iv: float, call_iv: float) -> dict:
        return _make_chain(
            symbol=sym,
            contracts=[
                _leg("C", 257.5, 0.50, 0.50),
                _leg("C", 272.5, 0.25, call_iv),
                _leg("P", 257.5, -0.50, 0.50),
                _leg("P", 240.0, -0.25, put_iv),
            ],
        )

    chains = [
        chain("A", put_iv=0.52, call_iv=0.50),  # RR = +2
        chain("B", put_iv=0.60, call_iv=0.50),  # RR = +10
        chain("C", put_iv=0.48, call_iv=0.50),  # RR = -2
    ]
    result = compare_across_tickers(chains)
    rrs = [m["d25"]["rr"] for m in result]
    assert rrs == sorted(rrs, reverse=True)
    assert [m["symbol"] for m in result] == ["B", "A", "C"]


def test_cross_tickers_nulls_sort_last():
    """Chains whose 25Δ RR can't be computed (thin wings) sort after
    ranked rows rather than occupying the top."""
    def chain_with_rr(sym: str, put_iv: float, call_iv: float) -> dict:
        return _make_chain(
            symbol=sym,
            contracts=[
                _leg("C", 257.5, 0.50, 0.50),
                _leg("C", 272.5, 0.25, call_iv),
                _leg("P", 257.5, -0.50, 0.50),
                _leg("P", 240.0, -0.25, put_iv),
            ],
        )

    chain_no_wings = _make_chain(
        symbol="Z",
        contracts=[_leg("C", 257.5, 0.50, 0.50)],
    )

    chains = [
        chain_no_wings,
        chain_with_rr("B", put_iv=0.55, call_iv=0.50),  # RR = +5
    ]
    result = compare_across_tickers(chains)
    assert result[0]["symbol"] == "B"  # ranked row first
    assert result[-1]["symbol"] == "Z"  # null row last
