"""``schwab_cli order ...`` subcommand handlers.

Phase 1 supports:

* ``order place``    — equity (single leg) and option (single or multi-leg
                       via ``--leg`` / ``--parse``). LIMIT, MARKET,
                       NET_DEBIT, NET_CREDIT.
* ``order preview``  — alias for ``order place --dry-run``. Renders the
                       confirmation panel and exits without sending
                       ``placeOrder``.
* ``order get``      — fetch one order by id.
* ``order list``     — synthetic ``--status=ACTIVE`` (default) expands to
                       all in-flight statuses, filtered client-side.
* ``order cancel``   — DELETE one order by id.

The confirmation flow (place / cancel) requires the user to type
``"yes"`` (case-insensitive) unless ``--yes`` is passed. Even with
``--yes`` the panel renders so the user has a record of what was sent.

**Safety**: tests for this module mock every Schwab call. Never let
production code path place an order without an explicit user
confirmation step gated by either ``--yes`` or the typed ``yea``.
"""

from __future__ import annotations

import json as _json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import typer

from schwab_cli import audit
from schwab_cli import config as config_module
from schwab_cli import order_limits
from schwab_cli.analytics.strategy_legs import LegParseError, Leg, parse_leg
from schwab_cli.api.client import AccountIds, ApiError, SchwabClient, SessionExpired
from schwab_cli.api.orders import (
    cancel_order,
    get_order,
    list_orders_all_accounts,
    list_orders_for_account,
    place_order,
    preview_order,
)
from schwab_cli.history_spec import RangeSpecError, parse_range
from schwab_cli.order_pipeline import (
    DEFAULT_RULES, PipelineContext, PipelineExit, run_pipeline,
)
from schwab_cli.order_policy.decision import Decision
from schwab_cli.order_policy.fields import OrderContext
from schwab_cli.order_policy.loader import select_profile_name
from schwab_cli.order_policy.sources import referenced_fields, required_sources
from schwab_cli.order_ticket import (
    ParsedLeg,
    ParsedTicket,
    TicketParseError,
    parse_ticket,
    to_osi,
)
from schwab_cli.output.orders import (
    PreviewSummary,
    render_order_detail_human,
    render_order_detail_json,
    render_order_list_human,
    render_order_list_json,
    summarise_preview,
)
from schwab_cli.session import load as load_session

# ---- exit codes (per docs/plan/order.md) ---------------------------------

EXIT_USAGE = 2
EXIT_NETWORK = 1
EXIT_REJECTED = 3
EXIT_POLICY_REJECTED = 4

# Schwab-accepted ``orderType`` values (Trader API §placeOrder).
_VALID_ORDER_TYPES: frozenset[str] = frozenset({
    "MARKET", "LIMIT", "STOP", "STOP_LIMIT",
    "TRAILING_STOP", "TRAILING_STOP_LIMIT",
    "MARKET_ON_CLOSE", "LIMIT_ON_CLOSE",
    "EXERCISE",
    "NET_DEBIT", "NET_CREDIT", "NET_ZERO",
})

# Synthetic --status categories → list of Schwab status enums.
STATUS_CATEGORIES: dict[str, tuple[str, ...]] = {
    "ACTIVE": (
        "WORKING", "PENDING_ACTIVATION", "QUEUED", "NEW", "ACCEPTED",
        "AWAITING_PARENT_ORDER", "AWAITING_CONDITION",
        "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW",
        "AWAITING_RELEASE_TIME", "AWAITING_UR_OUT",
        "PENDING_ACKNOWLEDGEMENT", "PENDING_RECALL",
    ),
    "FILLED": ("FILLED",),
    "CANCELED": ("CANCELED", "PENDING_CANCEL"),
    "REPLACED": ("REPLACED", "PENDING_REPLACE"),
    "REJECTED": ("REJECTED",),
    "EXPIRED": ("EXPIRED",),
    "ALL": (),  # sentinel: no client-side filter
}


# ---- shared helpers ------------------------------------------------------


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    return SchwabClient(cfg, session)


def _resolve_account_required(client: SchwabClient, user_input: str | None) -> AccountIds:
    if not user_input:
        typer.secho(
            "--account is required for place/preview/cancel.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    try:
        return client.resolve_account(user_input)
    except ApiError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)


def _handle_api_error(e: Exception, *, code: int = EXIT_NETWORK) -> None:
    msg = str(e) if str(e) else type(e).__name__
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _audit(subcommand: str, stage: str, **fields: object) -> None:
    """Convenience wrapper — flatten kwargs into an audit row."""
    payload: dict[str, object] = {"subcommand": subcommand, "stage": stage}
    payload.update(fields)
    audit.write_event(payload)


def _render_policy_decision(decision: Decision) -> None:
    """Render the per-policy decision table to stderr."""
    color = typer.colors.GREEN if decision.approved else typer.colors.RED
    typer.secho(
        f"\nPolicy Check  (profile: {decision.profile_name})",
        fg=typer.colors.BRIGHT_WHITE, err=True,
    )
    typer.echo("-" * 50, err=True)
    for ev in decision.evaluations:
        if not ev.enabled:
            typer.secho(f"  · {ev.name:<30}  disabled", err=True, dim=True)
            continue
        if not ev.matched:
            typer.echo(f"  · {ev.name:<30}  skipped (no match)", err=True)
            continue
        glyph = "✓" if ev.satisfied else "✗"
        glyph_color = typer.colors.GREEN if ev.satisfied else typer.colors.RED
        suffix = "conditions ✓" if ev.satisfied else "conditions failed"
        typer.secho(f"  {glyph} {ev.name:<30}  matched, {suffix}",
                    fg=glyph_color, err=True)
        for p in ev.predicates:
            if not p.satisfied:
                detail = (
                    f"      {p.field} {p.op} {p.expected!r} "
                    f"actual={p.actual!r}"
                )
                if p.error:
                    detail += f"  [{p.error}]"
                if p.unevaluatable:
                    detail += "  [unavailable in current phase]"
                typer.secho(detail, fg=typer.colors.RED, err=True)
    # Map internal verbs to past-participle policy vocabulary so the
    # display lines up with the engine's allow/deny semantics:
    #   "approve" → APPROVED   (the order is allowed through)
    #   "reject"  → DENIED     (the order was blocked by a deny rule
    #                          or fell through the default_action: deny)
    label = {"approve": "APPROVED"}.get(decision.decision, "DENIED")
    typer.secho(
        f"  Decision: {label}",
        fg=color, bold=True, err=True,
    )


def _run_override_path(
    *,
    body: dict,
    account: AccountIds,
    prof,
    override_reason: str,
    sub: str,
) -> None:
    """Single-ceremony override (Phase 2f-2).

    On entry the panel has already been rendered. The ceremony is:

    1. Print the override banner (account, reason).
    2. Fire a Telegram notification when ``prof.notify_on_override``
       is true and Telegram is configured. Best-effort; doesn't gate.
    3. Prompt for typed ``OVERRIDE`` (case-sensitive whole word).
    4. Audit ``override_invoked``.

    Anything other than typed ``OVERRIDE`` raises ``typer.Exit(0)``
    (aborted). The post-ceremony ``yea`` prompt and the actual place
    are handled by the caller.
    """
    typer.secho(
        f"\n!! OVERRIDE PATH — bypassing policy {prof.name!r}",
        fg=typer.colors.RED, err=True, bold=True,
    )
    typer.secho(f"   reason:  {override_reason}",
                fg=typer.colors.RED, err=True)

    if prof.notify_on_override:
        try:
            _send_override_notification(
                reason=override_reason, body=body,
                account=account, prof=prof,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            _audit(
                sub, "override_notify_failed",
                account=account.account_number,
                error=f"{type(e).__name__}: {e}",
            )

    _override_typed_prompt()

    _audit(
        sub, "override_invoked",
        account=account.account_number,
        profile_name=prof.name,
        override_reason=override_reason,
    )


def _override_typed_prompt() -> None:
    """Block until the user types literal ``OVERRIDE`` (case-sensitive
    full word). Anything else aborts (exit 0)."""
    typer.echo('Type "OVERRIDE" (case-sensitive) to bypass policy:',
               err=True, nl=False)
    typer.echo(" ", err=True, nl=False)
    try:
        entered = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        typer.echo("\naborted", err=True)
        raise typer.Exit(code=0)
    if not re.fullmatch(r"\s*OVERRIDE\s*", entered):
        typer.echo("aborted", err=True)
        raise typer.Exit(code=0)


def _send_override_notification(
    *, reason: str, body: dict, account: AccountIds, prof,
) -> None:
    """Fire-and-forget Telegram message announcing an override
    invocation. Skipped silently when Telegram isn't configured."""
    from schwab_cli.notify.config import load as load_notify_config
    from schwab_cli.notify import telegram as _send

    cfg = load_notify_config()
    bot_token = (cfg.telegram.bot_token or "").strip()
    chat_id = (cfg.telegram.chat_id or "").strip()
    if not (bot_token and chat_id):
        return

    legs_summary = []
    for leg in body.get("orderLegCollection") or []:
        instr = leg.get("instruction", "?")
        qty = leg.get("quantity", "?")
        sym = (leg.get("instrument") or {}).get("symbol", "?")
        legs_summary.append(f"{instr} {qty} {sym}")
    msg = (
        "⚠️ *OVERRIDE INVOKED*\n"
        f"Account: ********{account.account_number[-4:]}\n"
        f"Profile: {prof.name}\n"
        f"Order: {body.get('orderType', '?')} "
        f"{body.get('price') or ''}\n"
        f"Legs: {' / '.join(legs_summary) or '(none)'}\n"
        f"Reason: {reason}"
    )
    _send.send(bot_token=bot_token, chat_id=chat_id, text=msg)


def _confirm_or_abort(*, yes: bool) -> None:
    """Block until the user types 'yes' (case-insensitive). With ``yes=True``,
    skip the prompt entirely. Anything other than 'yes' aborts (exit 0).

    The caller is responsible for any leading blank line / live-ticker
    output above the prompt — keeping the prompt itself a single line
    means the ticker's row math (``\\x1b[1A`` to repaint one row up) is
    deterministic.
    """
    if yes:
        typer.echo("(--yes: skipping confirmation prompt)", err=True)
        return
    typer.echo('Type "yes" to confirm:', err=True, nl=False)
    typer.echo(" ", err=True, nl=False)
    try:
        entered = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        typer.echo("\naborted", err=True)
        raise typer.Exit(code=0)
    if not re.fullmatch(r"\s*yes\s*", entered, re.IGNORECASE):
        typer.echo("aborted", err=True)
        raise typer.Exit(code=0)


# ---- body builder --------------------------------------------------------


@dataclass(frozen=True)
class _NormalizedOrder:
    """Internal representation built from any input mode (flags, --leg,
    --parse). Body builder turns this into the Schwab JSON.
    """

    side: str                              # BUY/SELL (single-symbol view)
    quantity: int
    underlying: str
    order_type: str                        # LIMIT/MARKET/NET_DEBIT/NET_CREDIT
    duration: str                          # DAY/GOOD_TILL_CANCEL/...
    session: str                           # NORMAL/AM/PM/SEAMLESS
    price: float | None
    complex_strategy: str                  # NONE/VERTICAL/CUSTOM/...
    legs: tuple[dict, ...]                 # Schwab leg dicts
    strategy_label: str                    # human label for the panel
    is_naked_short: bool
    # For analytics:
    option_type: str | None                # CALL/PUT/None
    strikes: tuple[float, ...]


def _build_body(spec: _NormalizedOrder) -> dict:
    """Translate :class:`_NormalizedOrder` into the Schwab API body."""
    body: dict = {
        "session": spec.session,
        "duration": spec.duration,
        "orderType": spec.order_type,
        "complexOrderStrategyType": spec.complex_strategy,
        "quantity": spec.quantity,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": list(spec.legs),
    }
    if spec.price is not None:
        # Schwab prefers prices as strings to avoid float-truncation.
        body["price"] = f"{spec.price:.2f}"
    return body


def _equity_leg(side: str, quantity: int, symbol: str) -> dict:
    return {
        "instruction": side,
        "quantity": quantity,
        "instrument": {"assetType": "EQUITY", "symbol": symbol.upper()},
    }


def _option_leg(
    instruction: str, quantity: int,
    underlying: str, expiry, option_type: str, strike: float,
    *,
    position_effect_explicit: bool = False,
) -> dict:
    leg: dict = {
        "instruction": instruction,
        "quantity": quantity,
        "instrument": {
            "assetType": "OPTION",
            "symbol": to_osi(underlying, expiry, option_type, strike),
        },
    }
    if position_effect_explicit:
        # User-driven OPEN/CLOSE — set Schwab's positionEffect field so
        # the value is on the wire AND so DetectOpenCloseRule treats
        # the leg as "do not auto-rewrite".
        leg["positionEffect"] = (
            "OPENING" if instruction.endswith("_TO_OPEN") else "CLOSING"
        )
    return leg


def _spec_from_ticket(t: ParsedTicket) -> _NormalizedOrder:
    if t.is_equity:
        return _NormalizedOrder(
            side=t.side,
            quantity=t.quantity,
            underlying=t.underlying,
            order_type=t.order_type,
            duration=t.duration,
            session="NORMAL",
            price=t.price,
            complex_strategy="NONE",
            legs=(_equity_leg(t.side, t.quantity, t.underlying),),
            strategy_label=f"{t.side} {t.quantity} {t.underlying} (EQUITY)",
            is_naked_short=False,
            option_type=None,
            strikes=(),
        )

    schwab_legs: list[dict] = []
    for leg in t.legs:
        schwab_legs.append(
            _option_leg(
                leg.instruction, leg.quantity,
                leg.underlying, leg.expiry, leg.option_type, leg.strike,
                position_effect_explicit=leg.effect_explicit,
            )
        )

    if t.strategy == "VERTICAL":
        complex_strategy = "VERTICAL"
        side_word = "DEBIT" if t.order_type == "NET_DEBIT" else "CREDIT"
        label = f"VERTICAL {t.option_type} {side_word}"
    else:
        complex_strategy = "NONE"
        label = f"{t.side} {t.quantity} {t.underlying} {t.option_type}"

    naked = _is_naked_short_options(t.legs)

    return _NormalizedOrder(
        side=t.side,
        quantity=t.quantity,
        underlying=t.underlying,
        order_type=t.order_type,
        duration=t.duration,
        session="NORMAL",
        price=t.price,
        complex_strategy=complex_strategy,
        legs=tuple(schwab_legs),
        strategy_label=label,
        is_naked_short=naked,
        option_type=t.option_type,
        strikes=t.strikes,
    )


def _is_naked_short_options(legs: tuple[ParsedLeg, ...]) -> bool:
    """Naked = a short leg with no offsetting long of the same side at
    a more-favorable strike. Cheap heuristic, not a full risk model."""
    short_legs = [
        l for l in legs if l.instruction in ("SELL_TO_OPEN", "SELL_TO_CLOSE")
    ]
    if not short_legs:
        return False
    for short in short_legs:
        protectors = [
            l for l in legs
            if l.option_type == short.option_type
            and l.expiry == short.expiry
            and l.instruction in ("BUY_TO_OPEN", "BUY_TO_CLOSE")
        ]
        if short.option_type == "CALL":
            covered = any(p.strike >= short.strike for p in protectors)
        else:  # PUT — long PUT at a higher strike covers a short put
            covered = any(p.strike <= short.strike for p in protectors)
        if not covered:
            return True
    return False


def _spec_from_flags(
    *,
    symbol: str | None,
    side: str,
    quantity: int,
    order_type: str,
    price: float | None,
    duration: str,
    session: str,
    leg_specs: tuple[str, ...],
    complex_strategy: str,
) -> _NormalizedOrder:
    if leg_specs:
        # Multi-leg option order via --leg.
        if not symbol:
            typer.secho(
                "--leg requires a SYMBOL (the underlying).",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)
        try:
            parsed_legs = [parse_leg(s) for s in leg_specs]
        except LegParseError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_USAGE)
        underlying = symbol.upper()
        schwab_legs = tuple(
            _option_leg(
                l.instruction, abs(l.qty), underlying,
                l.expiry, "CALL" if l.side == "C" else "PUT", l.strike,
                position_effect_explicit=l.effect_explicit,
            )
            for l in parsed_legs
        )
        # Build ParsedLeg-shaped data for naked-short heuristic.
        parsed_for_naked = tuple(
            ParsedLeg(
                instruction=l.instruction,
                quantity=abs(l.qty),
                underlying=underlying,
                expiry=l.expiry,
                option_type="CALL" if l.side == "C" else "PUT",
                strike=l.strike,
            )
            for l in parsed_legs
        )
        # complex_strategy: if AUTO, leave as NONE for now (Phase 1 doesn't
        # yet wire the existing classify() into the body — TODO follow-up).
        # Users can pass --complex VERTICAL/CUSTOM/etc. explicitly.
        cs = complex_strategy if complex_strategy != "AUTO" else "NONE"
        # For multi-leg, default order type to NET_DEBIT/NET_CREDIT inferred
        # from the side flag.
        ot = order_type
        if ot == "LIMIT" and len(parsed_legs) > 1:
            ot = "NET_DEBIT" if side == "BUY" else "NET_CREDIT"
        # Simple analytics shape for verticals (future: more strategies).
        opt_type = (
            "CALL" if all(l.side == "C" for l in parsed_legs)
            else ("PUT" if all(l.side == "P" for l in parsed_legs) else None)
        )
        strikes = tuple(l.strike for l in parsed_legs)
        return _NormalizedOrder(
            side=side, quantity=quantity, underlying=underlying,
            order_type=ot, duration=duration, session=session,
            price=price, complex_strategy=cs,
            legs=schwab_legs,
            strategy_label=f"{cs} ({len(parsed_legs)} legs)" if cs != "NONE" else f"{len(parsed_legs)} legs",
            is_naked_short=_is_naked_short_options(parsed_for_naked),
            option_type=opt_type,
            strikes=strikes,
        )

    # Equity single-leg.
    if not symbol:
        typer.secho(
            "SYMBOL is required for equity orders.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    return _NormalizedOrder(
        side=side, quantity=quantity, underlying=symbol.upper(),
        order_type=order_type, duration=duration, session=session,
        price=price, complex_strategy="NONE",
        legs=(_equity_leg(side, quantity, symbol),),
        strategy_label=f"{side} {quantity} {symbol.upper()} (EQUITY)",
        is_naked_short=False,
        option_type=None,
        strikes=(),
    )


def _validate_combo(
    *, parse_string: str | None, symbol: str | None,
    order_type: str | None, price: float | None, quantity: int | None,
    side: str | None, duration: str | None, leg_specs: tuple[str, ...],
    complex_strategy: str | None,
) -> None:
    """Mutex checks for incompatible flag combinations."""
    # ``--quantity`` is per-order top-level; for multi-leg orders each
    # ``--leg`` token has its own signed N. Mixing the two creates an
    # ambiguity (does --quantity 2 mean 2× each leg, or override the
    # leg's own N?) — reject explicitly so the user picks one form.
    if leg_specs and quantity is not None:
        typer.secho(
            "--quantity may not be combined with --leg "
            "(each --leg token already carries its own signed quantity).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)

    if not parse_string:
        return
    forbidden = []
    if symbol:
        forbidden.append("SYMBOL (positional)")
    if order_type is not None:
        forbidden.append("--type")
    if price is not None:
        forbidden.append("--price")
    if quantity is not None:
        forbidden.append("--quantity")
    if side is not None:
        forbidden.append("--side")
    if duration is not None:
        forbidden.append("--duration")
    if leg_specs:
        forbidden.append("--leg")
    if complex_strategy is not None:
        forbidden.append("--complex")
    if forbidden:
        typer.secho(
            "--parse may not be combined with: " + ", ".join(forbidden),
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)


def _validate_session_combo(
    *, session: str, order_type: str, duration: str,
) -> None:
    """Schwab only accepts AM/PM/SEAMLESS with LIMIT + DAY."""
    if session in ("AM", "PM", "SEAMLESS") and not (
        order_type == "LIMIT" and duration == "DAY"
    ):
        typer.secho(
            f"--session={session} requires --type=LIMIT and --duration=DAY",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)


def _validate_override_flags(
    *,
    override_reason: str | None,
    override_confirm: bool,
    yes: bool,
    dry_run: bool,
) -> None:
    """Phase 2e: ``--override REASON --override-confirm`` must be passed
    together; either alone is a usage error. ``--override`` may not
    combine with ``--yes`` (override is not routine; the user must
    type both ``OVERRIDE`` and ``yea``). And it can't be used with
    ``--dry-run`` either — preview already bypasses placement."""
    if not override_reason or not override_confirm:
        typer.secho(
            "--override and --override-confirm must both be set together "
            "(either alone has no effect by design).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    if yes:
        typer.secho(
            "--override may not be combined with --yes (override is not "
            "routine; type OVERRIDE and yea explicitly).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    if dry_run:
        typer.secho(
            "--override is meaningless with --dry-run / `order preview` "
            "(preview already skips placement).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)
    n = len(override_reason)
    if not (10 <= n <= 500):
        typer.secho(
            f"--override reason must be 10..500 characters, got {n}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)


# ---- run_place / run_preview ---------------------------------------------


def run_place(
    *,
    symbol: str | None,
    account: str | None,
    order_type: str | None,
    price: float | None,
    quantity: int | None,
    side: str | None,
    duration: str | None,
    session: str,
    leg_specs: tuple[str, ...],
    complex_strategy: str | None,
    special: str | None,
    parse_string: str | None,
    dry_run: bool,
    yes: bool,
    as_json: bool,
    profile: str | None = None,
    override_reason: str | None = None,
    override_confirm: bool = False,
) -> None:
    sub = "preview" if dry_run else "place"
    # Log the raw invocation BEFORE any validation so even bad-flag
    # calls leave a footprint we can audit later.
    _audit(
        sub, "invoked",
        account=account,
        profile=profile,
        flags={
            "symbol": symbol, "order_type": order_type, "price": price,
            "quantity": quantity, "side": side, "duration": duration,
            "session": session, "legs": list(leg_specs),
            "complex_strategy": complex_strategy, "special": special,
            "parse_string": parse_string, "yes": yes,
            "override_reason": (
                "<set>" if override_reason else None
            ),
            "override_confirm": override_confirm,
        },
    )

    # Validate override flag combination before doing anything else.
    overriding = bool(override_reason) or override_confirm
    if overriding:
        _validate_override_flags(
            override_reason=override_reason,
            override_confirm=override_confirm,
            yes=yes, dry_run=dry_run,
        )

    _validate_combo(
        parse_string=parse_string, symbol=symbol, order_type=order_type,
        price=price, quantity=quantity, side=side, duration=duration,
        leg_specs=leg_specs, complex_strategy=complex_strategy,
    )

    # Build the normalized spec from whichever input mode was used.
    if parse_string:
        try:
            ticket = parse_ticket(parse_string)
        except TicketParseError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_USAGE)
        spec = _spec_from_ticket(ticket)
    else:
        spec = _spec_from_flags(
            symbol=symbol,
            side=side or "BUY",
            quantity=quantity if quantity is not None else 1,
            order_type=order_type or "LIMIT",
            price=price,
            duration=duration or "DAY",
            session=session,
            leg_specs=leg_specs,
            complex_strategy=complex_strategy or "AUTO",
        )

    _validate_session_combo(
        session=spec.session, order_type=spec.order_type, duration=spec.duration,
    )

    # Validate --type value: only Schwab-accepted orderType strings
    # pass through. Catches the common mistake of passing a side word
    # ("BUY"/"SELL") to --type before we hit the wire.
    if spec.order_type not in _VALID_ORDER_TYPES:
        typer.secho(
            f"--type={spec.order_type!r} is not a valid order type. "
            f"Allowed: {', '.join(sorted(_VALID_ORDER_TYPES))}.",
            fg=typer.colors.RED, err=True,
        )
        if spec.order_type.upper() in {
            "BUY", "SELL", "BUY_TO_OPEN", "BUY_TO_CLOSE",
            "SELL_TO_OPEN", "SELL_TO_CLOSE", "SELL_SHORT",
        }:
            typer.secho(
                "  hint: use --side for the buy/sell instruction "
                "(e.g. `--side SELL`); --type is the order type.",
                fg=typer.colors.YELLOW, err=True,
            )
        raise typer.Exit(code=EXIT_USAGE)

    # Validate price requirement for non-MARKET orders.
    if spec.order_type in ("LIMIT", "NET_DEBIT", "NET_CREDIT") and spec.price is None:
        typer.secho(
            f"--type={spec.order_type} requires --price",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)

    if special:
        # Tack on into the body during build (handled here so spec stays minimal).
        # Mutated in place — _NormalizedOrder is frozen but its `legs` tuple
        # belongs to a built body, not the spec itself; we keep the special
        # instruction on the body dict.
        pass

    body = _build_body(spec)
    if special:
        body["specialInstruction"] = special

    # Account resolution (required for place/preview).
    client = _client()
    acct = _resolve_account_required(client, account)
    body["accountNumber"] = acct.account_number  # for our own logging; Schwab ignores

    sanitized = audit.sanitise_body(body)
    _audit(
        sub, "body_built",
        account=acct.account_number,
        body=sanitized,
        is_naked_short=spec.is_naked_short,
        strategy_label=spec.strategy_label,
    )

    # Limit rules check (Phase 1 stub returns empty).
    limits = order_limits.evaluate(body, account_number=acct.account_number)
    if limits.forbidden and not dry_run:
        for f in limits.findings:
            if f.effect == "forbid":
                typer.secho(
                    f"FORBIDDEN by limit rule {f.rule_name!r}: {f.message}",
                    fg=typer.colors.RED, err=True,
                )
        _audit(
            sub, "forbidden_by_limit",
            account=acct.account_number,
            findings=[
                {"rule": f.rule_name, "effect": f.effect, "message": f.message}
                for f in limits.findings
            ],
        )
        raise typer.Exit(code=EXIT_USAGE)

    # Hand off to the rule pipeline (single shared flow for preview +
    # place; rules opt in/out via applies()).
    profile_name = select_profile_name(
        flag=profile, env=os.environ.get("SCHWAB_CLI_PROFILE"),
    )
    pipe_ctx = PipelineContext(
        spec=spec, body=body, account=acct, client=client, sub=sub,
        dry_run=dry_run, yes=yes, overriding=overriding,
        profile_name=profile_name, override_reason=override_reason,
        as_json=as_json, limits=limits,
    )
    try:
        run_pipeline(DEFAULT_RULES, pipe_ctx)
    except PipelineExit as halt:
        if halt.exit_code:
            raise typer.Exit(code=halt.exit_code)
        return


def _safe_place(
    client: SchwabClient, acct: AccountIds, body: dict, *, audit_subcommand: str,
) -> tuple[str, object]:
    """Wrap :func:`place_order` with a verify-and-rollback safety net.

    Schwab's placeOrder is "fire-and-forget at the protocol level":
    the server may have accepted the order even if the HTTP response
    failed (timeout mid-request, connection reset, KeyboardInterrupt
    while we wait, etc.). We can't rely on the absence of a response
    body — Schwab's success path is also empty.

    Strategy when ``place_order`` raises **anything** (including
    ``KeyboardInterrupt``):

    1. Audit ``place_uncertainty`` so the failure is recorded before
       we go off and probe.
    2. Call :func:`list_orders_for_account` for the last few minutes
       and look for orders matching ``body``'s fingerprint.
    3. For each match in a non-terminal status, call
       :func:`cancel_order` to roll it back. Audit each attempt.
    4. Surface a loud stderr warning so the user knows to verify
       their account.
    5. Re-raise the original exception so the normal handler runs
       and the CLI exits non-zero.

    On success, returns the same ``(order_id, response)`` tuple as
    ``place_order``.
    """
    try:
        return place_order(client, acct.hash_value, body)
    except BaseException as e:  # noqa: BLE001 — see docstring; we want everything
        if _is_definitive_rejection(e):
            # Schwab actively returned a 4xx — the order is NOT in their
            # book, so a rollback would just chase ghosts. Let the
            # normal `rejected` handler in run_place run.
            raise
        _audit(
            audit_subcommand, "place_uncertainty",
            account=acct.account_number,
            error=f"{type(e).__name__}: {e}",
        )
        try:
            matches = _find_matching_recent_orders(client, acct, body)
        except Exception as verify_err:  # noqa: BLE001
            _audit(
                audit_subcommand, "rollback_verify_failed",
                account=acct.account_number,
                error=f"{type(verify_err).__name__}: {verify_err}",
            )
            typer.secho(
                f"!! place failed AND we could not verify with Schwab "
                f"({type(verify_err).__name__}). We cannot determine "
                "whether your order reached the broker. Check your "
                "Schwab account immediately.",
                fg=typer.colors.RED, err=True,
            )
            raise e

        if not matches:
            _audit(
                audit_subcommand, "rollback_no_match",
                account=acct.account_number,
            )
            typer.secho(
                "place failed; no matching recent orders found at Schwab "
                "(the order most likely never reached the broker).",
                fg=typer.colors.YELLOW, err=True,
            )
            raise e

        cancelled: list[str] = []
        failed: list[tuple[str, str]] = []
        for m in matches:
            mid = str(m.get("orderId", ""))
            _audit(
                audit_subcommand, "rollback_cancel_attempt",
                account=acct.account_number,
                order_id=mid, status=m.get("status"),
            )
            try:
                cancel_order(client, acct.hash_value, mid)
                _audit(
                    audit_subcommand, "rollback_cancelled",
                    account=acct.account_number, order_id=mid,
                )
                cancelled.append(mid)
            except Exception as c_err:  # noqa: BLE001
                _audit(
                    audit_subcommand, "rollback_cancel_failed",
                    account=acct.account_number, order_id=mid,
                    error=f"{type(c_err).__name__}: {c_err}",
                )
                failed.append((mid, str(c_err)))

        # Surface a single, prominent warning so the user can act.
        plural = "s" if len(matches) != 1 else ""
        typer.secho(
            f"!! place failed but found {len(matches)} matching order{plural} "
            f"on Schwab (likely placed despite the error).",
            fg=typer.colors.RED, err=True,
        )
        if cancelled:
            typer.secho(
                f"   cancelled: {', '.join(cancelled)}",
                fg=typer.colors.RED, err=True,
            )
        for mid, msg in failed:
            typer.secho(
                f"   could NOT cancel {mid}: {msg}",
                fg=typer.colors.RED, err=True,
            )
        typer.secho(
            "   Please verify your account before re-submitting.",
            fg=typer.colors.RED, err=True,
        )
        raise e


def _find_matching_recent_orders(
    client: SchwabClient, acct: AccountIds, body: dict,
    *, window_minutes: int = 5,
) -> list[dict]:
    """Return Schwab orders entered in the last ``window_minutes``
    matching ``body``'s shape and in a non-terminal status.

    Match criteria — kept conservative so we don't cancel an order
    the user actually wanted:

    * same number of legs
    * each leg matches on (instruction, quantity, instrument.symbol)
    * same orderType
    * same complexOrderStrategyType
    * price equal within $0.01 (or both absent for MARKET)
    * status NOT in {FILLED, CANCELED, REJECTED, EXPIRED, REPLACED}
    """
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    raw = list_orders_for_account(
        client, acct.hash_value, start=start, end=end,
    )
    out: list[dict] = []
    for o in raw:
        if not _orders_match(body, o):
            continue
        if (o.get("status") or "").upper() in {
            "FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"
        }:
            continue
        out.append(o)
    return out


def _orders_match(body: dict, schwab_order: dict) -> bool:
    if body.get("orderType") != schwab_order.get("orderType"):
        return False
    if body.get("complexOrderStrategyType", "NONE") != \
            (schwab_order.get("complexOrderStrategyType") or "NONE"):
        return False

    body_price = _price_to_float(body.get("price"))
    sw_price = _price_to_float(schwab_order.get("price"))
    if body_price is None and sw_price is None:
        pass
    elif body_price is None or sw_price is None:
        return False
    elif abs(body_price - sw_price) > 0.005:
        return False

    body_legs = body.get("orderLegCollection") or []
    sw_legs = schwab_order.get("orderLegCollection") or []
    if len(body_legs) != len(sw_legs):
        return False
    for bl, sl in zip(body_legs, sw_legs):
        if bl.get("instruction") != sl.get("instruction"):
            return False
        if int(bl.get("quantity", 0)) != int(sl.get("quantity", 0)):
            return False
        bl_sym = (bl.get("instrument") or {}).get("symbol")
        sl_sym = (sl.get("instrument") or {}).get("symbol")
        if bl_sym != sl_sym:
            return False
    return True


def _price_to_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_definitive_rejection(e: BaseException) -> bool:
    """An exception that means Schwab actively returned a 4xx after
    reading our request body — the order is NOT placed and no
    rollback is needed. Distinguishes from network timeouts /
    5xx / Ctrl+C where the order's fate is uncertain.
    """
    if isinstance(e, SessionExpired):
        # 401 after refresh attempt — caller never even reached the
        # order endpoint with valid auth.
        return True
    if not isinstance(e, ApiError):
        return False
    msg = str(e)
    if msg.startswith("network:"):
        # Network/transport error wrapped to ApiError — uncertain
        # (Schwab may have received the bytes despite our timeout).
        return False
    return any(msg.startswith(code) for code in (
        "400 ", "401 ", "403 ", "404 ", "409 ", "422 ",
    ))


def _fetch_preview(
    client: SchwabClient, account_hash: str, body: dict,
) -> tuple[PreviewSummary, bool, dict | None]:
    """Call previewOrder; return ``(summary, unavailable_flag, raw)``.

    The raw response is also returned so the policy engine's
    field provider can read fields like ``buyingPowerEffect`` /
    ``commission`` directly without re-summarising.

    If Schwab's preview endpoint 4xx/5xx's, fall back to an empty
    summary with the unavailable flag set so the panel can render
    'unavailable' and the caller can still proceed.
    """
    try:
        raw = preview_order(client, account_hash, body)
        return summarise_preview(raw), False, raw
    except ApiError as e:
        msg = str(e)
        if msg.startswith(("404", "405", "501")):
            return summarise_preview(None), True, None
        # Other errors (500, network, auth) propagate so we don't silently
        # send an order without a true validation pass.
        raise


def _build_policy_context(
    *,
    client: SchwabClient,
    body: dict,
    account: AccountIds,
    prof,
    preview_raw: dict | None,
    sub: str,
) -> OrderContext:
    """Assemble an :class:`OrderContext` for the policy engine.

    Walks the active profile's enabled policies, computes the minimum
    set of data sources actually needed, and fetches each. Failures on
    optional fetches degrade to ``UnevaluatableField`` at policy-eval
    time; we don't propagate them out of here so a network glitch on
    a chain fetch can't take the whole place down.
    """
    needed = required_sources(referenced_fields(prof))

    chain_data: dict | None = None
    quote_data: dict | None = None
    account_data: dict | None = None
    dividend_data: dict | None = None
    counters_data = None
    transactions_data: list[dict] | None = None

    cats_underlying = _underlying_from_body(body)
    is_option = _is_option_order(body)

    if "chain" in needed:
        try:
            if is_option and cats_underlying:
                # Wide enough that the matched contract is in the response.
                chain_data = _fetch_chain_safe(client, cats_underlying)
            elif cats_underlying:
                quote_data = _fetch_quote_safe(client, cats_underlying)
        except Exception as e:  # noqa: BLE001 — best-effort
            _audit(sub, "policy_chain_fetch_failed",
                   account=account.account_number,
                   error=f"{type(e).__name__}: {e}")
    if "account" in needed:
        try:
            account_data = _fetch_account_safe(client, account.account_number)
        except Exception as e:  # noqa: BLE001
            _audit(sub, "policy_account_fetch_failed",
                   account=account.account_number,
                   error=f"{type(e).__name__}: {e}")
    if "dividends" in needed and cats_underlying:
        try:
            dividend_data = _fetch_dividend_safe(client, cats_underlying)
        except Exception as e:  # noqa: BLE001
            _audit(sub, "policy_dividend_fetch_failed",
                   account=account.account_number,
                   error=f"{type(e).__name__}: {e}")
    if "counters" in needed:
        try:
            from schwab_cli.order_policy import counters as _counters_mod
            counters_data = _counters_mod.load()
        except Exception as e:  # noqa: BLE001
            _audit(sub, "policy_counters_load_failed",
                   account=account.account_number,
                   error=f"{type(e).__name__}: {e}")
    if "transactions" in needed:
        try:
            transactions_data = _fetch_transactions_safe(
                client, account, hours=24,
            )
        except Exception as e:  # noqa: BLE001
            _audit(sub, "policy_transactions_fetch_failed",
                   account=account.account_number,
                   error=f"{type(e).__name__}: {e}")

    return OrderContext(
        body=body,
        account_number=account.account_number,
        today=date.today(),
        chain_data=chain_data,
        quote_data=quote_data,
        account_data=account_data,
        preview_data=preview_raw,
        dividend_data=dividend_data,
        counters_data=counters_data,
        transactions_data=transactions_data,
    )


def _underlying_from_body(body: dict) -> str | None:
    legs = body.get("orderLegCollection") or []
    if not legs:
        return None
    inst = (legs[0].get("instrument") or {})
    sym = inst.get("symbol", "")
    if (inst.get("assetType") or "").upper() == "OPTION":
        if len(sym) >= 21:
            return sym[:6].strip().upper() or None
        return sym.upper() or None
    return (sym or "").upper() or None


def _is_option_order(body: dict) -> bool:
    legs = body.get("orderLegCollection") or []
    if not legs:
        return False
    return (
        ((legs[0].get("instrument") or {}).get("assetType") or "").upper()
        == "OPTION"
    )


def _fetch_chain_safe(client: SchwabClient, symbol: str) -> dict:
    from schwab_cli.api.chains import get_chain
    return get_chain(client, symbol, contract_type="ALL", strike_count=20)


def _fetch_quote_safe(client: SchwabClient, symbol: str) -> dict:
    from schwab_cli.api.quotes import get_quotes
    return get_quotes(client, [symbol])


def _fetch_underlying_quote_safe(
    client: SchwabClient, body: dict,
) -> dict | None:
    """Fetch a normalized live quote for the order's underlying symbol.

    Returns ``None`` on any failure — the panel hides the section rather
    than poisoning the preview flow when the quote endpoint is down.
    """
    sym = _underlying_from_body(body)
    if not sym:
        return None
    try:
        from schwab_cli.api.quotes import get_quotes
        raw = get_quotes(client, [sym])
    except Exception:  # noqa: BLE001 — best-effort
        return None
    if not isinstance(raw, dict):
        return None
    entry = raw.get(sym)
    if not isinstance(entry, dict):
        return None
    q = entry.get("quote") if isinstance(entry.get("quote"), dict) else None
    if not q:
        return None
    return {
        "symbol": sym,
        "last": q.get("lastPrice"),
        "bid": q.get("bidPrice"),
        "ask": q.get("askPrice"),
        "bid_size": q.get("bidSize"),
        "ask_size": q.get("askSize"),
        "volume": q.get("totalVolume"),
        "net_change": q.get("netChange"),
    }


def _fetch_account_safe(client: SchwabClient, account_number: str) -> dict:
    from schwab_cli.api.accounts import get_account
    return get_account(client, account_number)


def _fetch_transactions_safe(
    client: SchwabClient, account: AccountIds, *, hours: int = 24,
) -> list[dict]:
    from schwab_cli.api.transactions import get_transactions
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=hours)
    return get_transactions(
        client, account.hash_value,
        start=start, end=end, types="TRADE",
    )


def _fetch_dividend_safe(client: SchwabClient, symbol: str) -> dict:
    """Fetch the fundamental block (carries ``nextDividendDate``)
    via the quotes endpoint with ``fields=all``."""
    from schwab_cli.api.quotes import get_quotes
    raw = get_quotes(client, [symbol], fields="all")
    if not isinstance(raw, dict):
        return {}
    # Re-shape into the {symbol: {nextDividendDate: ...}} form the
    # field provider expects. Schwab nests the dividend date inside
    # the per-symbol "fundamental" block.
    out: dict[str, dict] = {}
    for sym, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        fund = payload.get("fundamental") or {}
        next_div = fund.get("nextDividendDate")
        if next_div:
            out[sym] = {"nextDividendDate": next_div}
    return out


# ---- run_get -------------------------------------------------------------


def run_get(
    *, order_id: str, account: str | None, as_json: bool,
) -> None:
    _audit("get", "invoked", order_id=order_id, account=account)
    client = _client()
    acct = _resolve_account_for_read(client, account, action="get")

    try:
        order = get_order(client, acct.hash_value, order_id)
    except (ApiError, SessionExpired) as e:
        _audit(
            "get", "error",
            order_id=order_id, account=acct.account_number, error=str(e),
        )
        _handle_api_error(e)

    _audit(
        "get", "fetched",
        order_id=order_id, account=acct.account_number,
        status=order.get("status"),
    )

    if as_json:
        typer.echo(render_order_detail_json(order))
    else:
        typer.echo(render_order_detail_human(order))


# ---- run_list ------------------------------------------------------------


def run_list(
    *,
    account: str | None,
    status: str,
    range_str: str | None,
    limit: int | None,
    as_json: bool,
) -> None:
    _audit(
        "list", "invoked",
        account=account, status=status, range=range_str, limit=limit,
    )
    client = _client()

    # Resolve --range default based on --status.
    effective_range = range_str
    if effective_range is None:
        effective_range = "ALL" if status.upper() == "ACTIVE" else "-7d..now"

    # Translate ALL → 60-day window ending now (Schwab's max).
    if effective_range.upper() == "ALL":
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=60)
    else:
        try:
            start, end = parse_range(effective_range)
        except RangeSpecError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_USAGE)

    # Enforce 60-day cap client-side.
    if (end - start) > timedelta(days=60, hours=1):
        typer.secho(
            f"--range {effective_range!r} exceeds Schwab's 60-day max window.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=EXIT_USAGE)

    # Status: synthetic categories or raw enum.
    status_upper = status.upper()
    if status_upper in STATUS_CATEGORIES:
        # Synthetic: fetch unfiltered, filter client-side. (Schwab only
        # accepts a single status per call, so categories like ACTIVE
        # that union multiple statuses can't be done server-side.)
        wanted = set(STATUS_CATEGORIES[status_upper])
        raw_status_param: str | None = None
    else:
        wanted = set()  # no client-side filter needed
        raw_status_param = status_upper

    if account is None:
        typer.secho(
            "(no --account: querying across all accounts; "
            "passing --account is recommended)",
            fg=typer.colors.YELLOW, err=True,
        )
        try:
            orders = list_orders_all_accounts(
                client, start=start, end=end,
                status=raw_status_param, max_results=limit,
            )
        except (ApiError, SessionExpired) as e:
            _handle_api_error(e)
    else:
        try:
            acct = client.resolve_account(account)
        except ApiError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_USAGE)
        try:
            orders = list_orders_for_account(
                client, acct.hash_value, start=start, end=end,
                status=raw_status_param, max_results=limit,
            )
        except (ApiError, SessionExpired) as e:
            _handle_api_error(e)

    if wanted:
        orders = [o for o in orders if o.get("status") in wanted]

    _audit(
        "list", "fetched",
        account=account, status=status,
        effective_range=effective_range,
        result_count=len(orders),
    )

    if as_json:
        typer.echo(render_order_list_json(orders))
    else:
        typer.echo(render_order_list_human(orders))
        if effective_range.upper() == "ALL":
            typer.echo(
                "note: showing the last 60 days (Schwab API max). "
                "Older GTC orders not shown.",
                err=True,
            )


# ---- run_cancel ----------------------------------------------------------


def run_cancel(
    *,
    order_id: str,
    account: str | None,
    yes: bool,
    as_json: bool,
) -> None:
    _audit(
        "cancel", "invoked",
        order_id=order_id, account=account, yes=yes,
    )
    client = _client()
    try:
        acct, order = _find_order_for_cancel(client, account, order_id)
    except (ApiError, SessionExpired) as e:
        _audit("cancel", "lookup_failed", order_id=order_id, error=str(e))
        _handle_api_error(e)

    _audit(
        "cancel", "found",
        order_id=order_id, account=acct.account_number,
        status=order.get("status"),
    )

    typer.echo(
        f"About to cancel order {order_id} on account ...{acct.account_number[-4:]}:",
        err=True,
    )
    typer.echo(render_order_detail_human(order), err=True)

    try:
        if not yes:
            typer.echo("", err=True)  # separator between panel and prompt
        _confirm_or_abort(yes=yes)
    except typer.Exit as exit_:
        if int(exit_.exit_code or 0) == 0:
            _audit(
                "cancel", "aborted",
                order_id=order_id, account=acct.account_number,
            )
        raise
    _audit(
        "cancel", "confirmed",
        order_id=order_id, account=acct.account_number,
        via="--yes" if yes else "yea",
    )

    try:
        cancel_order(client, acct.hash_value, order_id)
    except (ApiError, SessionExpired) as e:
        _audit(
            "cancel", "cancel_failed",
            order_id=order_id, account=acct.account_number, error=str(e),
        )
        _handle_api_error(e)

    _audit(
        "cancel", "cancelled",
        order_id=order_id, account=acct.account_number,
    )

    if as_json:
        typer.echo(_json.dumps({"orderId": order_id, "status": "CANCEL_REQUESTED"}))
    else:
        typer.echo(f"cancel requested for order {order_id}", err=True)


def _find_order_for_cancel(
    client: SchwabClient, account_input: str | None, order_id: str,
) -> tuple[AccountIds, dict]:
    """Resolve an order id to (account, order_dict). When --account is
    given we hit one; otherwise we iterate every account and warn the
    user."""
    if account_input:
        acct = client.resolve_account(account_input)
        return acct, get_order(client, acct.hash_value, order_id)

    typer.secho(
        "(no --account: scanning every account for the order; "
        "passing --account is recommended)",
        fg=typer.colors.YELLOW, err=True,
    )
    last_err: Exception | None = None
    for acct in client.account_ids():
        try:
            return acct, get_order(client, acct.hash_value, order_id)
        except ApiError as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise ApiError(f"order {order_id} not found in any account")


def _resolve_account_for_read(
    client: SchwabClient, account_input: str | None, *, action: str,
) -> AccountIds:
    if account_input:
        try:
            return client.resolve_account(account_input)
        except ApiError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_USAGE)
    typer.secho(
        f"(no --account: using first available; "
        "passing --account is recommended for {action})".format(action=action),
        fg=typer.colors.YELLOW, err=True,
    )
    ids = client.account_ids()
    if not ids:
        typer.secho("no accounts found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)
    return ids[0]
