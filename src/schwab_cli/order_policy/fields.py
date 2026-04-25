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
from datetime import date, datetime
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

# Phase 2b-implemented fields (market data, strike-relative, pricing-
# relative, account state, BP impact, temporal, dividends).
PHASE_2B_FIELDS = frozenset({
    # Market data — chain/quote
    "spot", "bid", "ask", "mid", "mark",
    "delta", "gamma", "theta", "vega", "rho",
    "iv", "intrinsic", "extrinsic",
    # Strike-relative
    "strike_pct_of_spot", "strike_pct_above_spot", "strike_pct_below_spot",
    "moneyness",
    # Pricing-relative
    "price_pct_of_bid", "price_pct_of_ask",
    "price_pct_of_mid", "price_pct_of_mark",
    # Account state
    "net_liq", "cash", "bp_total", "bp_used", "bp_available",
    "bp_used_pct", "maint_req", "maint_cushion", "maint_cushion_pct",
    # BP impact (from preview)
    "bp_required", "bp_required_pct", "bp_after_pct",
    "order_value_pct_of_netliq",
    # Temporal
    "market_session", "minutes_since_open", "minutes_to_close",
    "is_market_holiday",
    # Dividends
    "days_to_ex_div",
})


@dataclass(frozen=True)
class OrderContext:
    """Snapshot of everything the field provider needs.

    Phase 2a fields (intrinsic + pricing) only need ``body`` /
    ``account_number`` / ``today``. Phase 2b adds optional prefetched
    payloads:

    * ``chain_data``      — Schwab chain payload for the option's
                            underlying (or a quote payload for the
                            equity). Carries spot + greeks + IV.
    * ``account_data``    — Schwab ``getAccount`` payload for the
                            target account (balances + positions).
    * ``preview_data``    — Schwab ``previewOrder`` JSON.
    * ``now_et``          — current ET datetime, injectable for
                            deterministic tests of temporal fields.
    * ``dividend_data``   — Schwab dividends payload for the
                            underlying (used by ``days_to_ex_div``).

    The provider raises :class:`UnevaluatableField` when a field
    needs a slot the caller didn't fill in. This keeps I/O out of
    the provider — the CLI handler decides what to fetch based on
    the active profile's field references.
    """

    body: dict                          # Schwab order JSON body
    account_number: str                 # plain account number (for `account` field)
    today: date                         # for `dte` calc — injectable in tests
    order_source: str = "manual"        # 'manual' | 'automated' | 'migration'

    # Phase 2b — optional prefetched payloads.
    chain_data: dict | None = None       # Schwab chain (option orders)
    quote_data: dict | None = None       # Schwab quote (equity orders or
                                         # underlying-only lookups)
    account_data: dict | None = None
    preview_data: dict | None = None
    now_et: "datetime | None" = None    # forward ref; lazy-resolved at use
    dividend_data: dict | None = None


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
        elif field_name in PHASE_2B_FIELDS:
            value = self._compute_2b(field_name)
        else:
            raise UnevaluatableField(
                f"field {field_name!r} not implemented "
                "(Phase 2c/2e or backlog)"
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

    # ====================================================================
    # Phase 2b — market data / strike-relative / pricing-relative /
    # account state / BP impact / temporal / dividends
    # ====================================================================

    def _compute_2b(self, name: str) -> Any:
        # Dispatch by family. The ``UnevaluatableField`` raised when the
        # caller didn't pre-fetch the right slot makes the missing-data
        # case observable in audit logs.
        if name in {"spot", "bid", "ask", "mid", "mark",
                    "delta", "gamma", "theta", "vega", "rho",
                    "iv", "intrinsic", "extrinsic"}:
            return self._md(name)
        if name in {"strike_pct_of_spot", "strike_pct_above_spot",
                    "strike_pct_below_spot", "moneyness"}:
            return self._strike_relative(name)
        if name.startswith("price_pct_of_"):
            return self._price_pct(name[len("price_pct_of_"):])
        if name in {"net_liq", "cash", "bp_total", "bp_used", "bp_available",
                    "bp_used_pct", "maint_req", "maint_cushion",
                    "maint_cushion_pct"}:
            return self._account_state(name)
        if name in {"bp_required", "bp_required_pct", "bp_after_pct"}:
            return self._bp_impact(name)
        if name == "order_value_pct_of_netliq":
            return self._order_value_pct_of_netliq()
        if name in {"market_session", "minutes_since_open",
                    "minutes_to_close", "is_market_holiday"}:
            return self._temporal(name)
        if name == "days_to_ex_div":
            return self._days_to_ex_div()
        raise UnevaluatableField(f"internal: no Phase 2b provider for {name!r}")

    # ---- market data (chain/quote) ----------------------------------

    def _md(self, name: str) -> Any:
        """Resolve a market-data field. Options use the chain payload's
        matching contract; the underlying spot comes from
        ``chain_data.underlying.last`` (option) or
        ``quote_data.{symbol}.quote.lastPrice`` (equity)."""
        # spot is the underlying — same path for option + equity.
        if name == "spot":
            return self._spot()

        if self._cats["asset_type"] == "OPTION":
            return self._md_from_option_contract(name)
        # Equity orders: bid/ask/last from quote payload; greeks N/A.
        return self._md_from_quote(name)

    def _spot(self) -> float:
        # Prefer chain_data when present (already includes the underlying).
        if self._ctx.chain_data:
            spot = (
                (self._ctx.chain_data.get("underlying") or {}).get("last")
            )
            if isinstance(spot, (int, float)):
                return float(spot)
        # Fall back to quote_data.
        if self._ctx.quote_data:
            sym = self._cats["underlying"]
            quote = (self._ctx.quote_data.get(sym) or {}).get("quote") or {}
            last = quote.get("lastPrice")
            if isinstance(last, (int, float)):
                return float(last)
        raise UnevaluatableField(
            "spot needs chain_data or quote_data — neither was prefetched"
        )

    def _md_from_option_contract(self, name: str) -> Any:
        contract = self._matched_option_contract()
        # Map our field names to the raw chain field names.
        path: dict[str, str] = {
            "bid": "bid", "ask": "ask", "mark": "mark",
            "delta": "delta", "gamma": "gamma",
            "theta": "theta", "vega": "vega", "rho": "rho",
            "intrinsic": "intrinsicValue",
        }
        if name == "mid":
            b = contract.get("bid"); a = contract.get("ask")
            if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
                raise UnevaluatableField("mid needs both bid and ask")
            return (float(b) + float(a)) / 2.0
        if name == "iv":
            v = contract.get("volatility")
            if not isinstance(v, (int, float)):
                raise UnevaluatableField("iv (volatility) missing from contract")
            return float(v)  # Schwab returns IV as a percent (e.g. 25.4)
        if name == "extrinsic":
            v = contract.get("timeValue")
            if not isinstance(v, (int, float)):
                raise UnevaluatableField("extrinsic (timeValue) missing")
            return float(v)
        if name in path:
            v = contract.get(path[name])
            if not isinstance(v, (int, float)):
                raise UnevaluatableField(
                    f"{name} ({path[name]}) missing from contract"
                )
            return float(v)
        raise UnevaluatableField(f"unknown option field {name!r}")

    def _md_from_quote(self, name: str) -> Any:
        if not self._ctx.quote_data:
            raise UnevaluatableField(
                f"{name} needs quote_data — not prefetched"
            )
        sym = self._cats["underlying"]
        quote = (self._ctx.quote_data.get(sym) or {}).get("quote") or {}
        path = {
            "bid": "bidPrice", "ask": "askPrice", "mark": "mark",
            "mid": None,
        }
        if name == "mid":
            b = quote.get("bidPrice"); a = quote.get("askPrice")
            if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
                raise UnevaluatableField("mid needs both bid and ask")
            return (float(b) + float(a)) / 2.0
        if name in path:
            v = quote.get(path[name])
            if not isinstance(v, (int, float)):
                raise UnevaluatableField(f"{name} ({path[name]}) missing from quote")
            return float(v)
        # Equity has no greeks / IV / intrinsic / extrinsic.
        raise UnevaluatableField(
            f"{name} not defined for EQUITY orders"
        )

    def _matched_option_contract(self) -> dict:
        """Find the contract in chain_data that matches the order's
        first option leg. Match by OSI symbol — exact equality."""
        if not self._ctx.chain_data:
            raise UnevaluatableField(
                "chain_data was not prefetched — option market-data fields unavailable"
            )
        sym = self._first_option_symbol(self._ctx.body.get("orderLegCollection") or [])
        if sym is None:
            raise UnevaluatableField(
                "no option leg on this order — option market-data fields unavailable"
            )
        # Walk both call and put exp-date maps looking for the matching
        # symbol. Schwab stores the OSI in `contract.symbol` with a
        # space-padded underlying; tolerate trailing-space variations.
        target = sym.strip()
        for source_key in ("callExpDateMap", "putExpDateMap"):
            date_map = self._ctx.chain_data.get(source_key) or {}
            for _exp_key, strike_map in date_map.items():
                for _strike, contracts in (strike_map or {}).items():
                    for c in contracts or []:
                        cand = (c.get("symbol") or "").strip()
                        if cand == target:
                            return c
        raise UnevaluatableField(
            f"contract {sym!r} not found in chain_data — broaden the chain "
            "fetch window or check OSI"
        )

    # ---- strike-relative ---------------------------------------------

    def _strike_relative(self, name: str) -> Any:
        spot = self.get("spot")
        try:
            strike = self.get("strike")
        except UnevaluatableField as e:
            raise UnevaluatableField(f"{name} needs option strike: {e}") from e
        if name == "strike_pct_of_spot":
            if spot == 0:
                raise UnevaluatableField("spot is zero")
            return strike / spot * 100.0
        if name == "strike_pct_above_spot":
            if spot == 0:
                raise UnevaluatableField("spot is zero")
            return (strike - spot) / spot * 100.0
        if name == "strike_pct_below_spot":
            if spot == 0:
                raise UnevaluatableField("spot is zero")
            return (spot - strike) / spot * 100.0
        if name == "moneyness":
            if spot == 0:
                raise UnevaluatableField("spot is zero")
            pct_above = (strike - spot) / spot * 100.0
            side = self._cats.get("option_side")
            # OTM/ITM depends on side: a CALL with strike > spot is OTM.
            # We bucket by abs(distance) to keep moneyness side-agnostic
            # (matches the spec's definition that's symmetric around ATM).
            d = abs(pct_above)
            if d <= 1.0:
                return "atm"
            if d <= 5.0:
                # Distinguish ITM vs OTM by side.
                return _itm_or_otm(pct_above, side, threshold="otm")
            if d <= 20.0:
                return _itm_or_otm(pct_above, side, threshold="otm")
            return _itm_or_otm(pct_above, side, threshold="deep_otm")
        raise UnevaluatableField(f"unknown strike-relative field {name!r}")

    # ---- pricing-relative -------------------------------------------

    def _price_pct(self, anchor: str) -> Any:
        # anchor in {"bid","ask","mid","mark"}.
        price = self.get("price")
        if price is None:
            raise UnevaluatableField(
                f"price_pct_of_{anchor} needs an order price (MARKET orders defer)"
            )
        if anchor not in {"bid", "ask", "mid", "mark"}:
            raise UnevaluatableField(f"unknown price-pct anchor {anchor!r}")
        anchor_value = self.get(anchor)
        if anchor_value == 0:
            raise UnevaluatableField(f"{anchor} is zero")
        return float(price) / float(anchor_value) * 100.0

    # ---- account state ----------------------------------------------

    def _account_state(self, name: str) -> Any:
        if not self._ctx.account_data:
            raise UnevaluatableField(
                f"{name} needs account_data (getAccount) — not prefetched"
            )
        # Schwab payload: {"securitiesAccount": {"currentBalances": {...},
        #   "initialBalances": {...}, "positions": [...]}}.
        sa = self._ctx.account_data.get("securitiesAccount") or {}
        cur = sa.get("currentBalances") or {}
        # Field aliases — Schwab's API returns slightly different names
        # for cash vs margin accounts; we read both and prefer the one
        # that's set.
        def _pick(*keys: str) -> Any:
            for k in keys:
                if k in cur and cur[k] is not None:
                    return cur[k]
            return None

        if name == "net_liq":
            v = _pick("liquidationValue", "equity")
        elif name == "cash":
            v = _pick("cashBalance", "totalCash", "cashAvailableForTrading")
        elif name == "bp_total":
            v = _pick("buyingPower", "dayTradingBuyingPower")
        elif name == "bp_used":
            v = _pick(
                "longMarketValue", "shortMarketValue",  # not perfect but workable
            )
            # If the API gives us a direct "buyingPowerUsed", prefer it.
            override = _pick("buyingPowerUsed")
            if override is not None:
                v = override
            elif (cur.get("longMarketValue") is not None
                  and cur.get("shortMarketValue") is not None):
                v = float(cur.get("longMarketValue", 0)) + abs(
                    float(cur.get("shortMarketValue", 0))
                )
        elif name == "bp_available":
            bp_total = self.get("bp_total")
            bp_used = self.get("bp_used")
            if not isinstance(bp_total, (int, float)) or not isinstance(bp_used, (int, float)):
                raise UnevaluatableField(
                    "bp_available needs bp_total and bp_used"
                )
            return float(bp_total) - float(bp_used)
        elif name == "bp_used_pct":
            bp_total = self.get("bp_total")
            bp_used = self.get("bp_used")
            if not isinstance(bp_total, (int, float)) or float(bp_total) == 0:
                raise UnevaluatableField("bp_total is zero or missing")
            return float(bp_used) / float(bp_total) * 100.0
        elif name == "maint_req":
            v = _pick("maintenanceRequirement")
        elif name == "maint_cushion":
            net_liq = self.get("net_liq")
            maint = self.get("maint_req")
            if not isinstance(net_liq, (int, float)) or not isinstance(maint, (int, float)):
                raise UnevaluatableField(
                    "maint_cushion needs net_liq and maint_req"
                )
            return float(net_liq) - float(maint)
        elif name == "maint_cushion_pct":
            net_liq = self.get("net_liq")
            cushion = self.get("maint_cushion")
            if not isinstance(net_liq, (int, float)) or float(net_liq) == 0:
                raise UnevaluatableField("net_liq is zero or missing")
            return float(cushion) / float(net_liq) * 100.0
        else:
            raise UnevaluatableField(f"unknown account field {name!r}")

        if not isinstance(v, (int, float)):
            raise UnevaluatableField(
                f"{name} not present in account_data.currentBalances"
            )
        return float(v)

    # ---- BP impact (preview-derived) ---------------------------------

    def _bp_impact(self, name: str) -> Any:
        if not self._ctx.preview_data:
            raise UnevaluatableField(
                f"{name} needs preview_data — not prefetched"
            )
        impact = (self._ctx.preview_data.get("orderValueImpact")
                  or self._ctx.preview_data.get("accountImpact") or {})
        if name == "bp_required":
            # A reduction in BP shows up as a negative effect; we expose
            # the magnitude as `bp_required` (always positive).
            v = impact.get("buyingPowerEffect")
            if v is None:
                v = self._ctx.preview_data.get("buyingPowerEffect")
            if not isinstance(v, (int, float)):
                raise UnevaluatableField(
                    "buyingPowerEffect missing from preview"
                )
            return abs(float(v))
        if name == "bp_required_pct":
            req = self.get("bp_required")
            try:
                bp_total = self.get("bp_total")
            except UnevaluatableField:
                # Without account data we can't compute the percentage.
                raise UnevaluatableField(
                    "bp_required_pct needs both preview_data and account_data"
                )
            if float(bp_total) == 0:
                raise UnevaluatableField("bp_total is zero")
            return float(req) / float(bp_total) * 100.0
        if name == "bp_after_pct":
            try:
                used_pct = self.get("bp_used_pct")
                req_pct = self.get("bp_required_pct")
            except UnevaluatableField:
                raise UnevaluatableField(
                    "bp_after_pct needs both preview_data and account_data"
                )
            return float(used_pct) + float(req_pct)
        raise UnevaluatableField(f"unknown BP-impact field {name!r}")

    def _order_value_pct_of_netliq(self) -> float:
        # Always derivable: order_value (intrinsic) / net_liq (account).
        ov = self.get("order_value")
        nl = self.get("net_liq")
        if float(nl) == 0:
            raise UnevaluatableField("net_liq is zero")
        return abs(float(ov)) / float(nl) * 100.0

    # ---- temporal ----------------------------------------------------

    def _temporal(self, name: str) -> Any:
        from schwab_cli.order_policy._calendar import session_status
        now = self._now_et()
        info = session_status(now)
        if name == "market_session":
            return info.session
        if name == "is_market_holiday":
            return info.is_holiday
        if name == "minutes_since_open":
            if info.session != "REGULAR":
                raise UnevaluatableField(
                    "minutes_since_open is only defined during REGULAR session"
                )
            return info.minutes_since_open
        if name == "minutes_to_close":
            if info.session != "REGULAR":
                raise UnevaluatableField(
                    "minutes_to_close is only defined during REGULAR session"
                )
            return info.minutes_to_close
        raise UnevaluatableField(f"unknown temporal field {name!r}")

    def _now_et(self) -> datetime:
        if self._ctx.now_et is not None:
            return self._ctx.now_et
        # Default — current wall clock in ET.
        from zoneinfo import ZoneInfo
        return datetime.now(tz=ZoneInfo("America/New_York"))

    # ---- dividends ---------------------------------------------------

    def _days_to_ex_div(self) -> int:
        if not self._ctx.dividend_data:
            raise UnevaluatableField(
                "days_to_ex_div needs dividend_data — not prefetched"
            )
        # The shape returned by api/dividends; we tolerate either a
        # dict-of-symbols (multi-symbol fetch) or a single-symbol dict.
        sym = self._cats["underlying"]
        d = self._ctx.dividend_data
        if isinstance(d, dict) and sym in d:
            d = d[sym]
        if not isinstance(d, dict):
            raise UnevaluatableField(
                f"dividend_data shape unexpected for {sym!r}"
            )
        # Schwab fundamental payload uses "nextDividendDate" — ISO date.
        next_iso = d.get("nextDividendDate") or d.get("ex_div_date")
        if not isinstance(next_iso, str):
            raise UnevaluatableField(
                f"no nextDividendDate for {sym!r} (does it pay a dividend?)"
            )
        try:
            ex = date.fromisoformat(next_iso[:10])
        except ValueError:
            raise UnevaluatableField(
                f"unparseable ex-div date {next_iso!r}"
            )
        return (ex - self._ctx.today).days


def _itm_or_otm(pct_above: float, side: str | None, *, threshold: str) -> str:
    """Bucket-name resolver shared by `moneyness`. ``pct_above`` is
    `(strike-spot)/spot*100` so a positive value means the strike is
    above spot (good for a CALL = OTM, good for a PUT = ITM)."""
    is_otm = (side == "C" and pct_above > 0) or (side == "P" and pct_above < 0)
    if threshold == "deep_otm":
        return "deep_otm" if is_otm else "deep_itm"
    return "otm" if is_otm else "itm"
