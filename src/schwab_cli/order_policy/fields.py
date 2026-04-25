"""Field provider — derives policy fields from the order body + account.

**Phase 2a**: only **intrinsic + pricing** fields are implemented.
Other categories (live market data, account state, BP impact,
position state, temporal, counters, strategy metadata) raise
:class:`UnevaluatableField` so policies referencing them produce
clear "not yet implemented" rows in the audit log.

Phase 2b will subclass this provider with chain/quote/getAccount
fetches; Phase 2c with counters + position state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from schwab_cli.order_policy.conditions import UnevaluatableField

# Categorical fields that the match clause needs (per spec §6.6).
CATEGORICAL_FIELDS = (
    "account", "underlying", "asset_type", "option_side",
    "instruction", "order_type", "duration", "session",
    "complex_strategy_type", "order_source",
)

# Phase 2a-implemented fields (categorical + intrinsic + pricing).
PHASE_2A_FIELDS = frozenset({
    *CATEGORICAL_FIELDS,
    "quantity", "strike", "expiry", "dte",
    "price", "order_value",
})


@dataclass(frozen=True)
class OrderContext:
    """Snapshot of everything Phase 2a's field provider needs.

    Phase 2b/2c will extend this (or wrap it) with live data
    snapshots; the categorical view + body+account remain stable.
    """

    body: dict                          # Schwab order JSON body
    account_number: str                 # plain account number (for `account` field)
    today: date                         # for `dte` calc — injectable in tests
    order_source: str = "manual"        # 'manual' | 'automated' | 'migration'


def categorical_view(ctx: OrderContext) -> dict[str, Any]:
    """Flat dict of the categorical fields used by the match clause."""
    body = ctx.body
    legs = body.get("orderLegCollection") or []

    asset_type = "EQUITY"
    option_side: str | None = None
    underlying: str | None = None
    instruction: str | None = None

    if legs:
        leg0 = legs[0]
        instr = (leg0.get("instrument") or {})
        asset_type = (instr.get("assetType") or "EQUITY").upper()
        instruction = leg0.get("instruction")

        if asset_type == "OPTION":
            sym = instr.get("symbol", "")
            # OSI 21-char: first 6 chars are the underlying (left-padded).
            if len(sym) >= 21:
                underlying = sym[:6].strip().upper()
                option_side = sym[12].upper() if len(sym) > 12 else None
            else:
                underlying = sym.upper()
        else:
            underlying = (instr.get("symbol") or "").upper() or None

    cstrat = body.get("complexOrderStrategyType")
    if not cstrat or cstrat == "NONE":
        cstrat = "VERTICAL" if len(legs) == 2 and asset_type == "OPTION" else "SINGLE"

    return {
        "account": ctx.account_number,
        "underlying": underlying,
        "asset_type": asset_type,
        "option_side": option_side,
        "instruction": instruction,
        "order_type": body.get("orderType"),
        "duration": body.get("duration"),
        "session": body.get("session", "NORMAL"),
        "complex_strategy_type": cstrat,
        "order_source": ctx.order_source,
    }


class FieldProvider:
    """Lazily resolves field values for the condition evaluator.

    ``ctx.get(field_name)`` is the callable handed to
    :func:`schwab_cli.order_policy.conditions.evaluate_condition`.
    Computed values are cached for the duration of one evaluation.
    """

    def __init__(self, ctx: OrderContext) -> None:
        self._ctx = ctx
        self._cache: dict[str, Any] = {}
        self._cats = categorical_view(ctx)

    def get(self, field_name: str) -> Any:
        if field_name in self._cache:
            return self._cache[field_name]
        if field_name in self._cats:
            value = self._cats[field_name]
        elif field_name in PHASE_2A_FIELDS:
            value = self._compute(field_name)
        else:
            raise UnevaluatableField(
                f"field {field_name!r} not available in Phase 2a "
                "(implemented in a later phase)"
            )
        self._cache[field_name] = value
        return value

    # ---- intrinsic + pricing fields ------------------------------------

    def _compute(self, name: str) -> Any:
        body = self._ctx.body
        legs = body.get("orderLegCollection") or []

        if name == "quantity":
            # Per spec §8.1: contract count or share count. For multi-
            # leg, surface the per-spread quantity (qty on the first
            # leg). For single-leg equity, the leg quantity is the
            # share count.
            if not legs:
                return body.get("quantity")
            return legs[0].get("quantity")

        if name == "strike":
            sym = self._first_option_symbol(legs)
            if sym is None:
                raise UnevaluatableField(
                    "strike is only defined for option orders"
                )
            # OSI: chars 13-20 = strike × 1000, zero-padded.
            try:
                return int(sym[13:21]) / 1000.0
            except (ValueError, IndexError):
                raise UnevaluatableField(
                    f"could not parse strike from OSI symbol {sym!r}"
                )

        if name == "expiry":
            sym = self._first_option_symbol(legs)
            if sym is None:
                raise UnevaluatableField(
                    "expiry is only defined for option orders"
                )
            try:
                yy = int(sym[6:8]); mm = int(sym[8:10]); dd = int(sym[10:12])
                # OSI YY → 20YY (the format covers 2000-2099)
                return date(2000 + yy, mm, dd).isoformat()
            except (ValueError, IndexError):
                raise UnevaluatableField(
                    f"could not parse expiry from OSI symbol {sym!r}"
                )

        if name == "dte":
            iso = self.get("expiry")
            try:
                exp = date.fromisoformat(iso)
            except ValueError:
                raise UnevaluatableField(
                    f"expiry {iso!r} not in ISO date format"
                )
            return (exp - self._ctx.today).days

        if name == "price":
            p = body.get("price")
            if p is None or p == "":
                return None
            try:
                return float(p)
            except (TypeError, ValueError):
                return None

        if name == "order_value":
            # Notional dollars. For equity: price * shares.
            # For options: price * 100 * contracts.
            price = self.get("price")
            if price is None:
                # Market order — notional is unknown without a quote;
                # defer to Phase 2b which has live mid.
                raise UnevaluatableField(
                    "order_value for MARKET orders needs live quote (Phase 2b)"
                )
            qty = self.get("quantity") or 0
            cats = self._cats
            multiplier = 100 if cats["asset_type"] == "OPTION" else 1
            return float(price) * float(qty) * multiplier

        # Should never reach here given PHASE_2A_FIELDS guard.
        raise UnevaluatableField(f"internal: no provider for {name!r}")

    @staticmethod
    def _first_option_symbol(legs: list[dict]) -> str | None:
        for leg in legs:
            inst = leg.get("instrument") or {}
            if (inst.get("assetType") or "").upper() == "OPTION":
                sym = inst.get("symbol")
                if isinstance(sym, str):
                    return sym
        return None
