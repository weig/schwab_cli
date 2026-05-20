"""Time-Weighted Return (TWR) computation.

Two layers:

- :func:`chain_link` — pure math, takes a list of ``DailyNav`` records
  (begin/end value + external cash flow) and returns the cumulative
  TWR over the series.
- :func:`reconstruct_positions` — replays a transaction history
  backwards from today's snapshot to produce per-day ``{symbol: shares}``
  and cash balances over a date range.

Cash-flow convention: ``external_flow > 0`` means money came INTO the
account from outside (deposit, ACATS in). ``< 0`` is a withdrawal.
Dividends, interest, and trades are NOT external flows — they're
internal returns and already reflected in the day's NAV change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo


_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DailyNav:
    """One day's NAV snapshot. ``value`` is end-of-day; ``external_flow``
    is the net deposit (positive) or withdrawal (negative) recorded on
    that day. ``external_flow`` is subtracted from ``value`` before the
    daily return is computed so cash movement doesn't fake performance.
    """
    day: date
    value: float
    external_flow: float = 0.0


def chain_link(navs: list[DailyNav]) -> float:
    """Chain-link daily Modified-Dietz-style sub-period returns.

    ``r_t = (V_t - CF_t) / V_{t-1} - 1``, then ``∏(1+r_t) - 1``.

    - Returns 0.0 for fewer than 2 points (no period to measure).
    - Skips days where the prior NAV is non-positive (can't divide by
      zero; the user's account was empty going into that day).
    """
    if len(navs) < 2:
        return 0.0
    cumulative = 1.0
    for i in range(1, len(navs)):
        bv = navs[i - 1].value
        if bv <= 0:
            continue
        ev = navs[i].value
        cf = navs[i].external_flow
        r = (ev - cf) / bv - 1.0
        cumulative *= (1.0 + r)
    return cumulative - 1.0


# ---- transaction → position/cash deltas -------------------------------


# Transaction types that are *candidates* for external flow. The actual
# classification is heuristic — see ``_is_external_flow`` — because
# ``RECEIVE_AND_DELIVER`` and ``JOURNAL`` cover both true external moves
# (ACATS, inter-account transfers) and internal events (option
# exercises, assignments, splits, spin-offs) that must NOT count as
# flow under TWR semantics.
_EXTERNAL_CANDIDATE_TYPES: frozenset[str] = frozenset({
    # User-initiated cash deposits/withdrawals at Schwab — these are
    # what the "Net Contributions" line on the Performance page sums.
    # Verified empirically by reconciling against the published total.
    "CASH_RECEIPT",
    "CASH_DISBURSEMENT",
    "WIRE_IN",
    "WIRE_OUT",
    "ACH_RECEIPT",
    "ACH_DISBURSEMENT",
    "ELECTRONIC_FUND",
    # ACATS-style transfers where Schwab books a pure-cash leg.
    # Internal-with-security variants are filtered out by the
    # has_security_leg test in `_is_external_flow`.
    "RECEIVE_AND_DELIVER",
    # NOTE: JOURNAL is intentionally *excluded*. At Schwab those are
    # internal bookkeeping movements (sweep MMF in/out, margin SMA
    # adjustments, sub-account journals) — never external flows. The
    # user's Performance page also excludes them.
})


def _is_external_flow(raw: dict, *, has_security_leg: bool) -> bool:
    """A transaction is an external cash flow only when:

    - its type is in :data:`_EXTERNAL_CANDIDATE_TYPES`, AND
    - it has NO non-fee security legs (no shares/contracts moved).

    The security-leg test is the key filter: option exercises and
    assignments are recorded as RECEIVE_AND_DELIVER but include the
    option contract leg AND a stock-receipt leg — neither is external,
    they're internal portfolio events. A pure cash transfer (ACATS
    cash, ACH, wire) has only a CURRENCY_USD leg.
    """
    t = (raw.get("type") or "").upper()
    if t not in _EXTERNAL_CANDIDATE_TYPES:
        return False
    return not has_security_leg


@dataclass(frozen=True)
class TxDelta:
    """The net effect of one Schwab transaction on positions + cash."""
    day: date
    cash_delta: float            # net cash change (positive = inflow)
    position_deltas: dict[str, float]  # {symbol: shares_delta}
    is_external_flow: bool       # if True, cash_delta also counts as
                                  # external flow for TWR purposes


def _ny_day(time_iso: str) -> date:
    """Parse Schwab's ``2026-05-15T20:49:16+0000`` and project to the
    NY trading day. NAV is bucketed by trading day, so any transaction
    on a given NY date contributes to that date's reconstruction."""
    s = time_iso.replace("Z", "+00:00")
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s).astimezone(_NY).date()


def parse_transaction(raw: dict) -> TxDelta | None:
    """Reduce one Schwab transaction payload to a ``TxDelta``.

    Returns ``None`` for malformed entries (missing time / netAmount).
    Options are folded in via their cash impact only — their position
    delta is recorded too, but the caller decides whether to value
    option positions (this v1 does not).
    """
    time_iso = raw.get("time")
    if not time_iso:
        return None
    try:
        day = _ny_day(time_iso)
    except ValueError:
        return None

    net = raw.get("netAmount")
    try:
        cash_delta = float(net) if net is not None else 0.0
    except (TypeError, ValueError):
        cash_delta = 0.0

    pos_deltas: dict[str, float] = {}
    for it in (raw.get("transferItems") or []):
        if it.get("feeType") is not None:
            continue  # fees affect cash via netAmount, not positions
        inst = it.get("instrument") or {}
        sym = inst.get("symbol")
        if not sym:
            continue
        if inst.get("assetType") == "CURRENCY" or sym == "CURRENCY_USD":
            continue  # cash leg, already in netAmount
        try:
            qty = float(it.get("amount") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0.0:
            continue
        # Schwab's amount sign convention: positive = received, negative =
        # delivered. ``positionEffect`` disambiguates short-sell vs cover.
        effect = (it.get("positionEffect") or "").upper()
        if effect in ("CLOSING",) and qty > 0:
            # Closing a short: shares delivered to cover, position goes up
            # (less negative). We treat qty as-is and rely on the sign
            # already reflecting "into the account".
            pass
        pos_deltas[sym] = pos_deltas.get(sym, 0.0) + qty

    is_external = _is_external_flow(raw, has_security_leg=bool(pos_deltas))
    return TxDelta(
        day=day,
        cash_delta=cash_delta,
        position_deltas=pos_deltas,
        is_external_flow=is_external,
    )


# ---- position reconstruction ------------------------------------------


@dataclass(frozen=True)
class DailyState:
    """End-of-day position + cash snapshot."""
    day: date
    positions: dict[str, float]
    cash: float
    external_flow: float  # external cash flow that occurred ON this day


def reconstruct_history(
    *,
    today: date,
    today_positions: dict[str, float],
    today_cash: float,
    transactions: Iterable[dict],
    days: list[date],
) -> list[DailyState]:
    """Walk transactions backwards from today to produce per-day state.

    Implementation: bucket transactions by NY trading day. Start at
    today and step backwards through ``days`` (which must be sorted
    descending or unsorted — we sort internally). At each step, undo
    every transaction that occurred AFTER the target day.

    External flows are recorded on the day they occurred so the TWR
    chain-link sees them on the correct sub-period.
    """
    days_sorted = sorted(set(days))  # ascending
    by_day: dict[date, list[TxDelta]] = {}
    for raw in transactions:
        delta = parse_transaction(raw)
        if delta is None:
            continue
        by_day.setdefault(delta.day, []).append(delta)

    # Start from today's snapshot and walk backwards. positions/cash
    # at end of day D = (positions/cash at end of D+1) - (deltas on D+1).
    pos = dict(today_positions)
    cash = float(today_cash)
    # External flow ON each day is the sum of external cash_deltas for
    # transactions stamped to that day. We compute it once, not by
    # walking backward, because the day-of-flow assignment doesn't
    # depend on which state we're at.
    ext_by_day: dict[date, float] = {}
    for day, deltas in by_day.items():
        ext_by_day[day] = sum(d.cash_delta for d in deltas if d.is_external_flow)

    # Anchor the snapshot at today, then step backward.
    states: dict[date, DailyState] = {
        today: DailyState(
            day=today, positions=dict(pos), cash=cash,
            external_flow=ext_by_day.get(today, 0.0),
        ),
    }

    # Build a reverse iteration starting from today minus one day,
    # consuming the deltas as we go. We only emit states for the days
    # the caller asked for (``days_sorted``), but we still must walk
    # day-by-day through every transaction date in-between to keep the
    # running state correct.
    all_days = sorted(
        {today} | set(by_day.keys()) | set(days_sorted),
        reverse=True,
    )
    for i in range(len(all_days) - 1):
        cur = all_days[i]      # later day
        prev = all_days[i + 1]  # earlier day
        # Undo all transactions that happened ON ``cur``: the state
        # at end of ``prev`` is end-of-``cur`` minus those deltas.
        for d in by_day.get(cur, []):
            cash -= d.cash_delta
            for sym, qty in d.position_deltas.items():
                pos[sym] = pos.get(sym, 0.0) - qty
                if abs(pos[sym]) < 1e-9:
                    pos.pop(sym, None)
        states[prev] = DailyState(
            day=prev, positions=dict(pos), cash=cash,
            external_flow=ext_by_day.get(prev, 0.0),
        )

    # Return only the requested days, in ascending order.
    out: list[DailyState] = []
    for d in days_sorted:
        if d in states:
            out.append(states[d])
        else:
            # No transactions touched dates between this and the nearest
            # later anchor — reuse the nearest later state's positions
            # (positions didn't change) but apply the day's own external
            # flow if any. In practice this branch is rare because we
            # seed every transaction day above.
            later = min((x for x in states if x >= d), default=None)
            if later is None:
                continue
            base = states[later]
            out.append(DailyState(
                day=d,
                positions=dict(base.positions),
                cash=base.cash,
                external_flow=ext_by_day.get(d, 0.0),
            ))
    return out


def simple_return(start_close: float, end_close: float) -> float:
    """Trivial point-to-point return. Used for index comparisons —
    indices have no cash flows so TWR collapses to (end / start - 1)."""
    if start_close <= 0:
        return 0.0
    return end_close / start_close - 1.0


# ---- realized P&L (FIFO) ----------------------------------------------


def realized_pl_fifo(transactions: list[dict]) -> float:
    """Realized P&L summed across all symbols via FIFO lot matching
    over a list of TRADE transactions.

    Method: walk transactions in time order; for each opening leg push
    a ``(qty, per_unit_cost)`` lot onto the symbol's queue; for each
    closing leg pop matching opposite-sign lots FIFO and accrue P&L.
    Closes with no matching open in the input list are skipped — their
    cost basis is unknown without extending the transaction window.

    Schwab payload conventions:
    - ``amount`` is signed: + when received, − when delivered.
    - ``cost`` is the leg's signed cash impact. We use ``|cost|/|qty|``
      as the per-unit price and reconstruct the P&L sign from lot
      direction (long vs short).
    """
    from collections import defaultdict, deque

    lots: dict[str, "deque[tuple[float, float]]"] = defaultdict(deque)
    realized = 0.0

    sorted_txns = sorted(
        (tx for tx in transactions
         if (tx.get("type") or "").upper() == "TRADE"),
        key=lambda t: t.get("time") or "",
    )
    for tx in sorted_txns:
        for leg in (tx.get("transferItems") or []):
            if leg.get("feeType") is not None:
                continue
            inst = leg.get("instrument") or {}
            sym = inst.get("symbol")
            atype = (inst.get("assetType") or "").upper()
            if not sym or atype == "CURRENCY" or sym == "CURRENCY_USD":
                continue
            try:
                qty = float(leg.get("amount") or 0)
                cost = float(leg.get("cost") or 0)
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            per_unit = abs(cost) / abs(qty)
            effect = (leg.get("positionEffect") or "").upper()
            if effect == "OPENING":
                lots[sym].append((qty, per_unit))
                continue
            if effect != "CLOSING":
                continue
            remaining = qty
            while abs(remaining) > 1e-9 and lots[sym]:
                open_qty, open_cost = lots[sym][0]
                if open_qty * remaining > 0:
                    break  # same side — not a matching lot
                matched = min(abs(open_qty), abs(remaining))
                if open_qty > 0:  # was long → close − open
                    realized += (per_unit - open_cost) * matched
                else:             # was short → open − close
                    realized += (open_cost - per_unit) * matched
                if matched + 1e-9 >= abs(open_qty):
                    lots[sym].popleft()
                else:
                    new_qty = open_qty + (
                        matched if open_qty < 0 else -matched
                    )
                    lots[sym][0] = (new_qty, open_cost)
                remaining += matched if remaining < 0 else -matched
    return realized


def classify_transactions(transactions: list[dict]) -> dict[str, float]:
    """Bucket transactions into the decomposition's change-factor totals.

    Returns::

        inflow         — Σ external cash flows where netAmount > 0
        outflow        — Σ external cash flows where netAmount < 0
        income         — Σ netAmount for DIVIDEND_OR_INTEREST
        fees           — Σ fee-leg costs (signed; typically negative)
    """
    out = {"inflow": 0.0, "outflow": 0.0, "income": 0.0, "fees": 0.0}
    for tx in transactions:
        t = (tx.get("type") or "").upper()
        try:
            net = float(tx.get("netAmount") or 0)
        except (TypeError, ValueError):
            net = 0.0
        for leg in (tx.get("transferItems") or []):
            if leg.get("feeType") is None:
                continue
            try:
                out["fees"] += float(leg.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        if t == "DIVIDEND_OR_INTEREST":
            out["income"] += net
            continue
        if t == "TRADE":
            continue
        if t in _EXTERNAL_CANDIDATE_TYPES:
            has_security = any(
                leg.get("feeType") is None
                and (leg.get("instrument") or {}).get("assetType") != "CURRENCY"
                and (leg.get("instrument") or {}).get("symbol")
                    not in (None, "CURRENCY_USD")
                for leg in (tx.get("transferItems") or [])
            )
            if has_security:
                continue
            if net > 0:
                out["inflow"] += net
            else:
                out["outflow"] += net
    return out
