"""Per-symbol volatility sampler — chain → metric bundle.

We feed the function a pre-loaded chain (no HTTP), so this test is
fully deterministic. The sampler returns a dict matching spec §5.1.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_cli.dataset.volatility import sample_volatility


_FIX = Path(__file__).parent / "fixtures"


def _load_chain(name: str) -> dict:
    return json.loads((_FIX / name).read_text())


def test_sample_volatility_returns_full_bundle():
    chain = _load_chain("chain_nvda_full.json")
    closes_30d = [200.0 + 0.5 * (i % 7) for i in range(60)]
    out = sample_volatility(chain=chain, underlying_closes=closes_30d)
    assert out["atm_iv_30d"] == pytest.approx(0.34, abs=0.001)
    assert out["atm_iv_60d"] is not None
    assert out["atm_iv_90d"] == pytest.approx(0.38, abs=0.001)
    assert out["iv_25d_put_30d"] == pytest.approx(0.36)
    assert out["iv_25d_call_30d"] == pytest.approx(0.32)
    assert out["hv_30d"] is not None
    assert out["raw_chain_summary"]["atm"]["30d"]["spot"] == 200.0


def test_sample_volatility_handles_missing_90d():
    chain = _load_chain("chain_nvda_full.json")
    chain["expiries"] = chain["expiries"][:2]
    out = sample_volatility(chain=chain, underlying_closes=[100.0] * 60)
    assert out["atm_iv_30d"] is not None
    assert out["atm_iv_60d"] is not None
    assert out["atm_iv_90d"] is None


def test_sample_volatility_skew_components_match_wing_iv():
    chain = _load_chain("chain_nvda_full.json")
    out = sample_volatility(chain=chain, underlying_closes=[100.0] * 60)
    skew_30 = out["iv_25d_put_30d"] - out["iv_25d_call_30d"]
    assert skew_30 == pytest.approx(0.04, abs=0.001)


def test_thin_chain_returns_bundle_with_nones():
    chain = _load_chain("chain_thin_no_deltas.json")
    out = sample_volatility(chain=chain, underlying_closes=[50.0] * 60)
    # Only 30 DTE present — 60 and 90 must be None.
    assert out["atm_iv_30d"] == pytest.approx(0.55)
    assert out["atm_iv_60d"] is None
    assert out["atm_iv_90d"] is None
    # No deltas → BS fallback uses atm_iv_30d (=0.55), should still
    # find a 25Δ-ish wing among the available strikes (or return None
    # gracefully). Either path is acceptable.
    assert out["iv_25d_put_30d"] == pytest.approx(0.65, abs=0.05) or \
           out["iv_25d_put_30d"] is None
    assert out["hv_30d"] is not None
