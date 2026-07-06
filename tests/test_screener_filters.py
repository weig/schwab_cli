"""Tests for the screener hard filters (§4) and config loading."""
from __future__ import annotations

from schwab_cli.screener.config import ScreenerConfig, load_screener_config
from schwab_cli.screener.filters import hard_filter_reason
from schwab_cli.storage.screener import ContractSnapshot

CFG = ScreenerConfig()


def _snap(**kw) -> ContractSnapshot:
    base = dict(
        snapshot_date="2026-07-06", symbol="X", captured_at_ms=1,
        put_strike=500.0, put_delta_actual=-0.25, put_bid=4.0, put_ask=4.2,
        put_mid=4.1, put_oi=1000, put_volume=300, spread_pct=0.049,
        underlying_last=540.0, atm_iv_30d=0.22, dte=31,
        next_earnings_date="2026-09-15", days_to_earnings=71,
    )
    base.update(kw)
    return ContractSnapshot(**base)


def test_survivor_passes_all():
    assert hard_filter_reason(_snap(), CFG) is None


def test_earnings_window():
    assert hard_filter_reason(
        _snap(next_earnings_date="2026-07-12", days_to_earnings=6), CFG
    ) == "earnings_window"


def test_earnings_unknown_fail_closed():
    assert hard_filter_reason(
        _snap(next_earnings_date=None, days_to_earnings=None), CFG
    ) == "earnings_unknown"


def test_earnings_unknown_allowed_when_not_required():
    cfg = ScreenerConfig(require_earnings_date=False)
    assert hard_filter_reason(
        _snap(next_earnings_date=None, days_to_earnings=None), cfg
    ) is None


def test_iv_out_of_range():
    assert hard_filter_reason(_snap(atm_iv_30d=4.0), CFG) == "iv_out_of_range"
    assert hard_filter_reason(_snap(atm_iv_30d=0.01), CFG) == "iv_out_of_range"


def test_iv_missing():
    assert hard_filter_reason(_snap(atm_iv_30d=None), CFG) == "iv_missing"


def test_spread_too_wide():
    assert hard_filter_reason(_snap(spread_pct=0.20), CFG) == "spread_too_wide"
    assert hard_filter_reason(_snap(spread_pct=None), CFG) == "spread_too_wide"


def test_oi_and_volume_and_price_and_bid_order():
    assert hard_filter_reason(_snap(put_oi=100), CFG) == "oi_too_low"
    assert hard_filter_reason(_snap(put_volume=10), CFG) == "volume_too_low"
    assert hard_filter_reason(_snap(underlying_last=20.0), CFG) == "underlying_too_low"
    assert hard_filter_reason(_snap(put_bid=0.05), CFG) == "bid_too_low"


def test_filter_order_earnings_before_liquidity():
    # A name failing multiple filters reports the FIRST (earnings) reason.
    s = _snap(next_earnings_date="2026-07-10", days_to_earnings=4,
              put_oi=1, put_volume=1)
    assert hard_filter_reason(s, CFG) == "earnings_window"


def test_load_config_defaults(monkeypatch):
    # No dataset.json present → pure defaults.
    monkeypatch.setattr(
        "schwab_cli.dataset.config.load_config_or_default", lambda: {}
    )
    cfg = load_screener_config()
    assert cfg.spread_pct_max == 0.10 and cfg.put_oi_min == 500


def test_load_config_overrides(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.dataset.config.load_config_or_default",
        lambda: {"screener": {"put_oi_min": 250, "rf_rate": 0.05, "bogus": 1}},
    )
    cfg = load_screener_config()
    assert cfg.put_oi_min == 250 and cfg.rf_rate == 0.05  # bogus ignored
