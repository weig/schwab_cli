"""Pre-canned policy shapes for `profile new` (Phase 2f-4).

Each template is a callable that takes a *prompter* (a small protocol
the editor passes in) and returns a policy dict that will be appended
to the profile's policy list.

Templates are deliberately decoupled from prompt_toolkit so they
can be tested with a stub prompter that returns canned answers.

Templates ship in 2f-4:

* allow_equity_trade
* allow_short_put_open
* allow_covered_call_open
* allow_vertical_spread
* deny_underlying
* deny_loss_cooldown
* deny_fat_finger
* custom

Two of the spec's deny templates (daily_cap, concentration_cap) are
deferred to follow-up patches; users author them via `custom` for now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol


class Prompter(Protocol):
    """Input abstraction for templates.

    The editor's real prompter wraps :func:`prompt_toolkit.prompt`;
    tests inject a deterministic stub.
    """

    def text(self, label: str, *, default: str = "") -> str: ...
    def select(self, label: str, choices: list[str], *,
               default: str | None = None) -> str: ...
    def integer(self, label: str, *,
                default: int | None = None,
                min_value: int | None = None) -> int | None: ...
    def number(self, label: str, *,
               default: float | None = None) -> float | None: ...
    def yes_no(self, label: str, *, default: bool = False) -> bool: ...


@dataclass(frozen=True)
class Template:
    """One template entry in the picker menu."""

    key: str
    label: str
    description: str
    build: Callable[[Prompter], dict]


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _slug(*parts: str) -> str:
    """Make a policy name safe for the schema (`[a-zA-Z0-9_.-]{1,64}`).

    Lowercases, replaces non-alnum with underscores, collapses runs.
    """
    raw = "_".join(parts).lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return cleaned[:64] or "policy"


def _ticker_list(prompter: Prompter, label: str) -> list[str]:
    raw = prompter.text(label).strip()
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


# ---- templates -----------------------------------------------------------


def _allow_equity_trade(p: Prompter) -> dict:
    tickers = _ticker_list(p, "Tickers (comma-separated)")
    side = p.select(
        "Side", ["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"],
        default="BUY",
    )
    qty_cap = p.integer("Max quantity (blank = no cap)", default=None,
                        min_value=1)
    name_suffix = "_".join(tickers).lower() if tickers else "all"
    pol: dict = {
        "name": _slug("allow", side.lower(), name_suffix),
        "match": {
            "underlying": tickers,
            "asset_type": ["EQUITY"],
            "instruction": [side],
        },
        "effect": "allow",
    }
    if qty_cap is not None:
        pol["conditions"] = [{"quantity": {"lte": qty_cap}}]
    return pol


def _allow_short_put_open(p: Prompter) -> dict:
    tickers = _ticker_list(p, "Tickers (comma-separated)")
    delta_lo = p.number("Delta low (negative)", default=-0.30)
    delta_hi = p.number("Delta high (negative or 0)", default=-0.10)
    dte_lo = p.integer("DTE low", default=21, min_value=0)
    dte_hi = p.integer("DTE high", default=90, min_value=1)
    iv_max = p.number("Max IV % (blank = no cap)", default=None)
    bp_pct_max = p.number(
        "Max bp_required_pct (blank = no cap)", default=None,
    )
    conditions: list[dict] = [
        {"delta": {"gte": delta_lo, "lte": delta_hi}},
        {"dte": {"gte": dte_lo, "lte": dte_hi}},
    ]
    if iv_max is not None:
        conditions.append({"iv": {"lte": iv_max}})
    if bp_pct_max is not None:
        conditions.append({"bp_required_pct": {"lte": bp_pct_max}})
    return {
        "name": _slug("allow_short_put_open",
                      *(t.lower() for t in tickers)),
        "match": {
            "underlying": tickers,
            "asset_type": ["OPTION"],
            "option_side": ["P"],
            "instruction": ["SELL_TO_OPEN"],
        },
        "conditions": conditions,
        "effect": "allow",
    }


def _allow_covered_call_open(p: Prompter) -> dict:
    tickers = _ticker_list(p, "Tickers (comma-separated)")
    dte_lo = p.integer("DTE low", default=21, min_value=0)
    dte_hi = p.integer("DTE high", default=60, min_value=1)
    strike_above_min = p.number(
        "Min strike_pct_above_spot (e.g. 3 for 3% OTM)", default=3.0,
    )
    return {
        "name": _slug("allow_covered_call_open",
                      *(t.lower() for t in tickers)),
        "match": {
            "underlying": tickers,
            "asset_type": ["OPTION"],
            "option_side": ["C"],
            "instruction": ["SELL_TO_OPEN"],
        },
        "conditions": [
            {"covered_by_equity": {"eq": True}},
            {"dte": {"gte": dte_lo, "lte": dte_hi}},
            {"strike_pct_above_spot": {"gte": strike_above_min}},
        ],
        "effect": "allow",
    }


def _allow_vertical_spread(p: Prompter) -> dict:
    side = p.select(
        "Net side", ["DEBIT", "CREDIT"], default="DEBIT",
    )
    tickers = _ticker_list(p, "Tickers (comma-separated; blank = any)")
    max_price = p.number("Max debit (or min credit) per spread", default=None)
    max_qty = p.integer("Max contracts per order", default=None, min_value=1)
    order_type = "NET_DEBIT" if side == "DEBIT" else "NET_CREDIT"
    match: dict = {
        "asset_type": ["OPTION"],
        "complex_strategy_type": ["VERTICAL"],
        "order_type": [order_type],
    }
    if tickers:
        match["underlying"] = tickers
    conditions: list[dict] = []
    if max_price is not None:
        if order_type == "NET_DEBIT":
            conditions.append({"price": {"lte": max_price}})
        else:
            conditions.append({"price": {"gte": max_price}})
    if max_qty is not None:
        conditions.append({"quantity": {"lte": max_qty}})
    return {
        "name": _slug("allow_vertical", side.lower(),
                      *(t.lower() for t in tickers)),
        "match": match,
        "conditions": conditions,
        "effect": "allow",
    }


def _deny_underlying(p: Prompter) -> dict:
    tickers = _ticker_list(p, "Tickers to BLOCK (comma-separated)")
    reason = p.text(
        "Reason (shown on rejection)",
        default=f"underlying in deny list: {', '.join(tickers)}",
    )
    return {
        "name": _slug("deny_underlying",
                      *(t.lower() for t in tickers)),
        "match": {"underlying": tickers},
        "effect": "deny",
        "reason": reason,
    }


def _deny_loss_cooldown(p: Prompter) -> dict:
    n = p.integer(
        "Block when consecutive_losing_closes_24h ≥ N",
        default=3, min_value=1,
    )
    return {
        "name": _slug("deny_loss_cooldown_ge", str(n)),
        "match": "*",
        "conditions": [{"consecutive_losing_closes_24h": {"gte": n}}],
        "effect": "deny",
        "reason": f"cool-down: {n}+ losses in last 24h",
    }


def _deny_fat_finger(p: Prompter) -> dict:
    lo = p.number("Min price_pct_of_mid (e.g. 70 for 70%)", default=70.0)
    hi = p.number("Max price_pct_of_mid (e.g. 130 for 130%)", default=130.0)
    qty_max = p.integer(
        "Max quantity per order (blank = no cap)", default=10, min_value=1,
    )
    or_terms: list[dict] = [
        {"price_pct_of_mid": {"lt": lo}},
        {"price_pct_of_mid": {"gt": hi}},
    ]
    if qty_max is not None:
        or_terms.append({"quantity": {"gt": qty_max}})
    return {
        "name": "deny_fat_finger",
        "match": "*",
        "conditions": [{"or": or_terms}],
        "effect": "deny",
        "reason": "fat-finger guard",
    }


def _custom(p: Prompter) -> dict:
    """Free-form path. Prompt for the four required fields directly
    as JSON snippets (advanced — users who pick this know the schema).
    """
    import json as _json

    name = p.text("Policy name (a-z0-9_, ≤64 chars)")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid policy name {name!r} — must match /^[a-z][a-z0-9_]{{0,63}}$/"
        )
    effect = p.select("Effect", ["allow", "deny"], default="allow")
    match_raw = p.text(
        'match clause (JSON; e.g. {"underlying": ["KO"]} or "*")',
        default="*",
    )
    try:
        match = _json.loads(match_raw)
    except _json.JSONDecodeError as e:
        raise ValueError(f"match is not valid JSON: {e}")
    conds_raw = p.text(
        "conditions (JSON list; e.g. [{\"dte\": {\"gte\": 21}}])",
        default="[]",
    )
    try:
        conditions = _json.loads(conds_raw)
    except _json.JSONDecodeError as e:
        raise ValueError(f"conditions is not valid JSON: {e}")
    if not isinstance(conditions, list):
        raise ValueError("conditions must be a JSON list")
    return {
        "name": name,
        "match": match,
        "conditions": conditions,
        "effect": effect,
    }


# Public registry. The order is the menu order.
TEMPLATES: tuple[Template, ...] = (
    Template("allow_equity_trade",
             "allow equity trade",
             "Single-leg equity buy/sell on a ticker list with optional qty cap.",
             _allow_equity_trade),
    Template("allow_short_put_open",
             "allow short put open",
             "SELL_TO_OPEN P with delta + DTE bounds, optional IV/BP caps.",
             _allow_short_put_open),
    Template("allow_covered_call_open",
             "allow covered call open",
             "SELL_TO_OPEN C with covered_by_equity + DTE + strike-OTM bounds.",
             _allow_covered_call_open),
    Template("allow_vertical_spread",
             "allow vertical spread",
             "NET_DEBIT / NET_CREDIT verticals with width + qty caps.",
             _allow_vertical_spread),
    Template("deny_underlying",
             "deny underlying",
             "Hard block on a ticker list (e.g. meme-stock blocklist).",
             _deny_underlying),
    Template("deny_loss_cooldown",
             "deny loss cooldown",
             "Block when N consecutive losing closes hit in last 24h.",
             _deny_loss_cooldown),
    Template("deny_fat_finger",
             "deny fat finger",
             "Block when price drifts > X% from mid OR quantity exceeds N.",
             _deny_fat_finger),
    Template("custom",
             "custom (advanced)",
             "Hand-roll a policy by entering match + conditions as JSON.",
             _custom),
)


def by_key(key: str) -> Template | None:
    for t in TEMPLATES:
        if t.key == key:
            return t
    return None
