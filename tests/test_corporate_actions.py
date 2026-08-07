"""Split detection + ticker-identity classification."""
from __future__ import annotations

from schwab_cli.analytics.corporate_actions import (
    classify_identity,
    detect_adjustment_ratio,
)


# ---- split / adjustment detection -----------------------------------------

def test_detects_a_clean_split_ratio():
    # A 4:1 split: fresh (adjusted) is 1/4 of cached (raw) on every shared day.
    cached = {"2026-06-29": 772.0, "2026-06-30": 780.0, "2026-07-01": 776.0}
    fresh = {"2026-06-29": 193.0, "2026-06-30": 195.0, "2026-07-01": 194.0}
    r = detect_adjustment_ratio(cached, fresh)
    assert r is not None and abs(r - 0.25) < 0.01


def test_no_adjustment_when_series_match():
    same = {"2026-06-29": 100.0, "2026-06-30": 101.0, "2026-07-01": 102.0}
    assert detect_adjustment_ratio(same, dict(same)) is None


def test_real_one_day_move_is_not_flagged_as_split():
    # CNC-style: one day crashed 40% (real), other days unchanged. The ratios
    # are NOT consistent → not a split.
    cached = {"d1": 56.0, "d2": 56.0, "d3": 34.0, "d4": 34.0}
    fresh = {"d1": 56.0, "d2": 56.0, "d3": 34.0, "d4": 34.0}   # already same
    assert detect_adjustment_ratio(cached, fresh) is None
    # And an inconsistent overlap (one day differs, others don't) → None.
    fresh2 = {"d1": 56.0, "d2": 56.0, "d3": 34.0, "d4": 20.0}
    assert detect_adjustment_ratio(cached, fresh2) is None


def test_requires_minimum_overlap():
    cached = {"d1": 100.0, "d2": 200.0}
    fresh = {"d1": 25.0, "d2": 50.0}    # 0.25 but only 2 days
    assert detect_adjustment_ratio(cached, fresh, min_overlap=3) is None


def test_ignores_non_positive_and_missing_days():
    cached = {"d1": 100.0, "d2": 0.0, "d3": 100.0, "d4": 100.0}
    fresh = {"d1": 50.0, "d2": 50.0, "d3": 50.0, "d5": 999.0}
    # d2 (cached 0) and d4/d5 (missing in one side) excluded → d1,d3 only = 2
    assert detect_adjustment_ratio(cached, fresh, min_overlap=3) is None


# ---- identity classification ----------------------------------------------

def test_first_sighting_is_new():
    assert classify_identity(None, None, "22788C105", "CROWDSTRIKE") == "new"


def test_same_cusip_is_ok():
    assert classify_identity("22788C105", "CROWDSTRIKE HLDGS Class A",
                             "22788C105", "CROWDSTRIKE HLDGS Class A") == "ok"


def test_reverse_split_same_issuer_is_corporate_action():
    # CUSIP issue digits change (…105 → …204) but issuer prefix 22788C stays.
    assert classify_identity("22788C105", "CROWDSTRIKE HLDGS INC",
                             "22788C204", "CROWDSTRIKE HLDGS INC") \
        == "corporate_action"


def test_name_match_rescues_when_prefix_differs():
    # Reincorporation could change the issuer prefix but keep the name.
    assert classify_identity("22788C105", "CROWDSTRIKE HLDGS INC",
                             "99999X100", "CROWDSTRIKE HLDGS INC") \
        == "corporate_action"


def test_different_company_is_reuse():
    # FIG: old delisted issuer → new unrelated company under the same ticker.
    assert classify_identity("11111A100", "OLD DELISTED CO",
                             "22222B200", "FIGMA INC") == "reuse"


def test_missing_current_cusip_does_not_quarantine():
    assert classify_identity("22788C105", "CROWDSTRIKE",
                             None, None) == "ok"


def test_class_share_change_is_same_company():
    # GOOG Class C (02079K107) ↔ Class A (02079K305): same issuer 02079K.
    assert classify_identity("02079K107", "ALPHABET INC Class C",
                             "02079K305", "ALPHABET INC A") \
        == "corporate_action"
