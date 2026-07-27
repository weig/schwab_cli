"""BUG-5: winsorized IVR + IVR/IVP divergence as a data-quality signal.

Plain IVR = (today - min) / (max - min) uses the two most extreme points, so
a single outlier collapses it (the GOOG incident: one 8.43 row drove IVR from
14.7 to 0.26). IVP is a rank statistic, immune to outliers. A large IVR/IVP
divergence is therefore itself a corruption signal — winsorizing the IVR
bounds to p1/p99 blunts the outlier, and flagging the divergence surfaces the
bad data instead of silently trusting it.
"""
from __future__ import annotations

from schwab_cli.service.vol import _ivr_from, ivr_ivp_quality


def test_winsorized_ivr_is_more_robust_than_raw_to_a_mild_outlier():
    """Winsorizing to p1/p99 shifts less than the raw IVR under a mild
    outlier. (p1/p99 can't fully neutralize a gross outlier at n~60 — that's
    INC-1's job — but it strictly improves on raw.)"""
    from schwab_cli.service.vol import _ivr_raw
    clean = [0.27 + 0.002 * i for i in range(60)]      # 0.27 .. 0.388
    today = 0.29
    win_shift = abs(_ivr_from(clean, today) - _ivr_from(clean + [0.55], today))
    raw_shift = abs(_ivr_raw(clean, today) - _ivr_raw(clean + [0.55], today))
    assert win_shift < raw_shift


def test_gross_outlier_is_flagged_by_divergence():
    """The actual incident diverged by ~20 (raw IVR ~0 vs IVP ~16); the
    detector must fire. A gross outlier is caught here, removed by INC-1."""
    clean = [0.27 + 0.002 * i for i in range(60)]
    q = ivr_ivp_quality(clean + [8.43], 0.29)
    assert q["data_quality_warning"] is True


def test_clean_skewed_series_does_not_false_positive():
    """A clean but skewed equity IV series (raw IVR ≈ IVP) must not trip."""
    clean = [0.27 + 0.002 * i for i in range(60)]
    q = ivr_ivp_quality(clean, 0.29)
    assert q["data_quality_warning"] is False


def test_ivr_from_clamps_to_0_100():
    series = [0.30, 0.32, 0.34, 0.36, 0.38]
    assert 0.0 <= _ivr_from(series, 0.20) <= 100.0   # below p1
    assert 0.0 <= _ivr_from(series, 0.50) <= 100.0   # above p99


def test_ivr_from_flat_series_returns_50():
    assert _ivr_from([0.3, 0.3, 0.3], 0.3) == 50.0


def test_divergence_flags_data_quality_warning():
    # Construct IVR≪IVP: one huge outlier tanks IVR but not IVP.
    series = [0.30] * 30 + [0.31] * 30 + [8.43]
    today = 0.305
    q = ivr_ivp_quality(series, today)
    assert q["data_quality_warning"] is True
    assert q["suspect_samples"]                      # lists the outlier(s)
    assert any(s["value"] > 3.0 for s in q["suspect_samples"])


def test_no_divergence_no_warning():
    series = [0.27 + 0.002 * i for i in range(60)]
    q = ivr_ivp_quality(series, 0.31)
    assert q["data_quality_warning"] is False
    assert q["suspect_samples"] == []


def test_suspect_samples_are_furthest_from_median():
    series = [0.30] * 40 + [0.31] * 40 + [8.43, 0.001]
    q = ivr_ivp_quality(series, 0.305)
    vals = [s["value"] for s in q["suspect_samples"]]
    assert 8.43 in vals and 0.001 in vals            # both extremes surfaced
