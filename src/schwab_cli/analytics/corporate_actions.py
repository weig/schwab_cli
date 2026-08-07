"""Corporate-action hygiene — split detection + ticker-identity classification.

Two independent, pure concerns (no I/O):

1. **Splits poison HV.** Schwab returns split-ADJUSTED history, but our cache is
   incremental — old rows keep pre-split values while new rows come back
   adjusted, leaving a spurious 1-day return at the split (CRWD's cache made
   HV30 read 399%). ``detect_adjustment_ratio`` compares overlapping cached vs
   freshly-fetched closes: a consistent, non-unit ratio means an adjustment
   happened and the whole series must be re-fetched. CUSIP-agnostic.

2. **Ticker reuse silently merges two companies.** ``classify_identity`` uses
   the CUSIP (6-char issuer prefix + issue) plus the issuer name to tell a
   same-company corporate action (reverse split / reorg can change the CUSIP
   under the same ticker) apart from an actual reuse (delisted ticker taken
   over by a different issuer). Ambiguity resolves to reuse → quarantine,
   because a false quarantine is recoverable and a false merge is silent
   corruption.
"""
from __future__ import annotations

# A consistent fresh/cached ratio outside this band = an adjustment event.
# Dividends nudge prices <1%; splits move them by tens of percent.
_UNIT_LO, _UNIT_HI = 0.98, 1.02
# Overlapping-day ratios must agree this tightly to count as ONE clean factor
# (a real price move perturbs a single day, not all overlap days uniformly).
_RATIO_CONSISTENCY = 0.02


def detect_adjustment_ratio(
    cached: dict[str, float],
    fresh: dict[str, float],
    *,
    min_overlap: int = 3,
) -> float | None:
    """Return the split/adjustment factor if the overlapping days shifted by a
    single consistent non-unit ratio, else ``None``.

    ``cached``/``fresh`` map day→close. Only days present in both (and with
    positive closes) are compared. Requires ``min_overlap`` common days so a
    single bad quote can't trigger a full re-fetch.
    """
    common = [d for d in cached
              if d in fresh and cached[d] > 0 and fresh[d] > 0]
    if len(common) < min_overlap:
        return None
    ratios = [fresh[d] / cached[d] for d in common]
    lo, hi = min(ratios), max(ratios)
    # All overlap days must move by ~the same factor (a real 1-day move would
    # spike one ratio while the others stay ~1).
    if hi - lo > _RATIO_CONSISTENCY:
        return None
    mid = sum(ratios) / len(ratios)
    if _UNIT_LO <= mid <= _UNIT_HI:
        return None            # within noise → no adjustment
    return mid


def _issuer_prefix(cusip: str | None) -> str | None:
    """The 6-char issuer portion of a CUSIP (stable per company)."""
    return cusip[:6] if cusip and len(cusip) >= 6 else None


def _norm_name(desc: str | None) -> str:
    """Normalize an issuer description for coarse same-company comparison —
    the issuer name minus share-class noise."""
    if not desc:
        return ""
    s = desc.upper()
    for junk in ("CLASS A", "CLASS B", "CLASS C", " A", " B", " C",
                 "INC", "CORP", "HLDGS", "HOLDINGS", "LTD", "PLC", "."):
        s = s.replace(junk, "")
    return " ".join(s.split())


def classify_identity(
    stored_cusip: str | None,
    stored_desc: str | None,
    cur_cusip: str | None,
    cur_desc: str | None,
) -> str:
    """Classify a symbol's current identity against what we recorded.

    Returns one of:
      * ``"new"``               — no prior record (first sighting)
      * ``"ok"``                — same CUSIP, keep appending
      * ``"corporate_action"``  — CUSIP changed but same issuer (prefix or
                                  name) → reverse split / reorg; re-fetch and
                                  update the stored CUSIP, do NOT quarantine
      * ``"reuse"``             — CUSIP changed and issuer differs → ticker
                                  taken over by a different company; quarantine
    """
    if not stored_cusip:
        return "new"
    if cur_cusip and cur_cusip == stored_cusip:
        return "ok"
    if not cur_cusip:
        # No identity to compare — don't guess; treat as ok to avoid
        # false quarantine on a transient missing field.
        return "ok"
    same_issuer = _issuer_prefix(cur_cusip) == _issuer_prefix(stored_cusip)
    same_name = (
        bool(_norm_name(cur_desc)) and _norm_name(cur_desc) == _norm_name(stored_desc)
    )
    if same_issuer or same_name:
        return "corporate_action"
    return "reuse"
