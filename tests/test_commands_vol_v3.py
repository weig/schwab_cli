"""3-tier IVR/IVP fallback in the vol command (spec §9).

TIER 1 — atm_iv_30d series ≥ 120 days
TIER 2 — atm_iv legacy series ≥ 120 days (may include synthetic rows)
TIER 3 — trigger backfill, retry tier 2; flag backfilled=True
"""
from __future__ import annotations

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.service.vol import compute_iv_rank_and_percentile


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as c:
        yield c


def _seed_atm_iv_30d(conn, n_days: int, value_seed: float = 0.30):
    from schwab_cli.storage.vol_history import record_extended_snapshot
    base_ms = 1_700_000_000_000
    for i in range(n_days):
        record_extended_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.34,
            atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
            captured_at_ms=base_ms + i * 86_400_000,
            atm_iv_30d=value_seed + 0.001 * i,
        )


def _seed_atm_iv_legacy(conn, n_days: int, atm_iv_30d_value=None):
    """Insert legacy-only rows (no atm_iv_30d unless explicitly provided)."""
    from schwab_cli.storage.vol_history import record_snapshot
    base_ms = 1_700_000_000_000
    for i in range(n_days):
        record_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.34 + 0.001 * i,
            atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
            captured_at_ms=base_ms + i * 86_400_000,
        )


def test_tier1_used_when_120_days_present(conn):
    _seed_atm_iv_30d(conn, n_days=130)
    out = compute_iv_rank_and_percentile(
        conn, symbol="NVDA", today_iv_30d=0.40,
        today_atm_iv=0.40,
    )
    assert out["source"] == "atm_iv_30d"
    assert out["backfilled"] is False
    assert out["n_days"] == 130


def test_tier2_used_when_atm_iv_30d_short(conn):
    _seed_atm_iv_30d(conn, n_days=50)
    _seed_atm_iv_legacy(conn, n_days=130)
    out = compute_iv_rank_and_percentile(
        conn, symbol="NVDA", today_iv_30d=0.40,
        today_atm_iv=0.40,
    )
    assert out["source"].startswith("atm_iv (legacy")


def test_tier3_returns_low_history_when_both_short(conn):
    _seed_atm_iv_legacy(conn, n_days=20)
    out = compute_iv_rank_and_percentile(
        conn, symbol="NVDA", today_iv_30d=0.40,
        today_atm_iv=0.40,
        backfill_callable=None,
    )
    assert out["ivr"] is None
    assert out["ivp"] is None
    assert out.get("low_history") is True


def test_render_shows_term_structure_and_skew():
    from schwab_cli.output.vol import render_vol_human
    snapshot = {
        "symbol": "NVDA", "as_of": "2026-04-26", "spot": 207.13,
        "atm_iv": 0.3412, "atm_dte": 5,
        "atm_iv_30d": 0.3581, "atm_iv_60d": 0.3624, "atm_iv_90d": 0.3689,
        "iv_25d_put_30d": 0.39, "iv_25d_call_30d": 0.34,
        "iv_25d_put_60d": 0.40, "iv_25d_call_60d": 0.35,
        "iv_25d_put_90d": 0.41, "iv_25d_call_90d": 0.36,
        "hv_30d": 0.284,
        "ivr_ivp": {
            "ivr": 41.3, "ivp": 34.1, "n_days": 248,
            "source": "atm_iv_30d", "backfilled": False,
        },
    }
    out = render_vol_human(snapshot)
    assert "ATM IV 30d:" in out
    assert "35.81" in out
    assert "HV  30d:" in out
    assert "Skew  30d:" in out
    assert "5.0 vol pts" in out  # 0.39 - 0.34 = 0.05 = 5.0 vol pts
    assert "atm_iv_30d, 248 days" in out


def test_render_marks_backfilled_with_warning():
    from schwab_cli.output.vol import render_vol_human
    snapshot = {
        "symbol": "X", "as_of": "2026-04-26", "spot": 100,
        "atm_iv": 0.5, "atm_dte": 7,
        "atm_iv_30d": None, "atm_iv_60d": None,
        "atm_iv_90d": None, "hv_30d": None,
        "iv_25d_put_30d": None, "iv_25d_call_30d": None,
        "iv_25d_put_60d": None, "iv_25d_call_60d": None,
        "iv_25d_put_90d": None, "iv_25d_call_90d": None,
        "ivr_ivp": {
            "ivr": 50.0, "ivp": 50.0, "n_days": 198,
            "source": "atm_iv (legacy + synthetic)", "backfilled": True,
        },
    }
    out = render_vol_human(snapshot)
    assert "backfilled" in out.lower()
