"""Rule classes for the order pipeline.

Each rule encapsulates one step of the order flow. The same ordered
list serves both ``order preview`` and ``order place`` — rules opt in
or out via ``applies()`` based on flags on the context.

Heavy lifting (Schwab API calls, prompts, audit emission) stays in
``commands/order.py`` helpers; rules are thin orchestration. Helpers
are imported lazily inside ``execute`` to avoid the circular import
between this module and ``commands/order``.
"""

from __future__ import annotations

import json as _json
from typing import Protocol

import typer

from .context import OrderContext, RuleResult


class OrderRule(Protocol):
    name: str

    def applies(self, ctx: OrderContext) -> bool: ...
    def execute(self, ctx: OrderContext) -> RuleResult: ...


# ---- exit codes (re-exported from commands.order) ------------------------


def _exit_codes():
    """Lazy import to avoid the circular dep at module load."""
    from schwab_cli.commands import order as _order_mod
    return _order_mod


# ---- rules --------------------------------------------------------------


class LoadProfileRule:
    """Step 1 — resolve the policy profile.

    On miss: ``preview`` warns and continues with ``profile=None``;
    ``place`` (and ``--override``) errors out.
    """

    name = "load_profile"

    def applies(self, ctx: OrderContext) -> bool:
        return True

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _audit, EXIT_USAGE
        from schwab_cli.order_policy import PolicyConfigError, load_profile
        try:
            ctx.profile = load_profile(ctx.profile_name)
        except PolicyConfigError as e:
            ctx.profile_load_error = str(e)
            _audit(
                ctx.sub, "policy_load_failed",
                account=ctx.account.account_number,
                profile=ctx.profile_name, error=str(e),
            )
            if ctx.dry_run and not ctx.overriding:
                typer.secho(
                    f"warning: no policy profile resolved ({e})",
                    fg=typer.colors.YELLOW, err=True,
                )
                typer.secho(
                    "  preview will show the order detail + Schwab preview "
                    "but skip the policy gate.",
                    fg=typer.colors.YELLOW, err=True,
                )
                return RuleResult()
            typer.secho(f"policy load failed: {e}",
                        fg=typer.colors.RED, err=True)
            return RuleResult(halt=True, exit_code=EXIT_USAGE)
        return RuleResult()


class FetchAccountBalancesRule:
    """Step 2 — pull current Stock/Option BP for the panel's BP triple.

    Skipped on ``place --yes`` (no panel = no need).
    """

    name = "fetch_account_balances"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _fetch_current_balances_safe
        ctx.current_balances = _fetch_current_balances_safe(
            ctx.client, ctx.account.account_number,
        )
        return RuleResult()


class FetchUnderlyingQuoteRule:
    """Step 2b — one-shot underlying quote for the Underlying section.

    Skipped on ``place --yes``. The is_live flag is set so the panel
    label can read "Live Quote" for the place-without-yes path.
    """

    name = "fetch_underlying_quote"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _fetch_underlying_quote_safe
        q = _fetch_underlying_quote_safe(ctx.client, ctx.body)
        if q is not None:
            q["is_live"] = (not ctx.dry_run)
        ctx.underlying_quote = q
        return RuleResult()


class SchwabPreviewRule:
    """Step 3 — call Schwab's previewOrder for commission, fees, BP, rejects.

    Skipped on ``place --yes`` per spec — the operator has already
    committed and won't review the panel.
    """

    name = "schwab_preview"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _audit, _fetch_preview
        ctx.preview_summary, ctx.preview_unavailable, ctx.raw_preview = (
            _fetch_preview(ctx.client, ctx.account.hash_value, ctx.body)
        )
        _audit(
            ctx.sub,
            "preview_unavailable" if ctx.preview_unavailable else "preview_ok",
            account=ctx.account.account_number,
            commission=ctx.preview_summary.commission,
            fees=ctx.preview_summary.fees,
            bp_after_stock=ctx.preview_summary.bp_after_stock,
            bp_after_option=ctx.preview_summary.bp_after_option,
            warnings=list(ctx.preview_summary.warnings),
            rejects=list(ctx.preview_summary.rejects),
        )
        # Diagnostic shape capture on any reject (see commands/order.py
        # comment for context — distinguishes hard/soft rejects later).
        if (not ctx.preview_unavailable
                and ctx.raw_preview is not None
                and ctx.preview_summary.rejects):
            _audit(
                ctx.sub, "preview_reject_shape",
                account=ctx.account.account_number,
                validation=ctx.raw_preview.get("orderValidationResult"),
                order_status=(ctx.raw_preview.get("orderStrategy") or {}).get("status"),
                order_balance=(ctx.raw_preview.get("orderStrategy") or {}).get("orderBalance"),
            )
        return RuleResult()


class ComputeAnalyticsRule:
    """Compute the at-expiry analytics block (max profit/loss, etc.)."""

    name = "compute_analytics"

    def applies(self, ctx: OrderContext) -> bool:
        # Always — even --yes path benefits from analytics in audit log.
        return True

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.output.orders import compute_analytics
        ctx.analytics = compute_analytics(
            strategy=(
                ctx.spec.complex_strategy
                if ctx.spec.complex_strategy != "NONE"
                else None
            ),
            side=ctx.spec.side,
            option_type=ctx.spec.option_type,
            strikes=ctx.spec.strikes,
            quantity=ctx.spec.quantity,
            price=ctx.spec.price,
        )
        return RuleResult()


class RenderPanelRule:
    """Build the confirmation panel string. Doesn't emit yet — that's the
    next rule's job — so the panel can be re-rendered with live ticks."""

    name = "render_panel"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.output.orders import PreviewSummary, render_confirmation
        # Even when preview was skipped (shouldn't happen with current
        # applies() set, but be defensive), render with an empty summary.
        preview = ctx.preview_summary or PreviewSummary(
            None, None, None, None, (), (),
        )
        ctx.panel_text = render_confirmation(
            body=ctx.body,
            account_tail=ctx.account.account_number[-4:],
            strategy_label=ctx.spec.strategy_label,
            is_naked_short=ctx.spec.is_naked_short,
            analytics=ctx.analytics,
            preview=preview,
            preview_unavailable=ctx.preview_unavailable,
            underlying_quote=ctx.underlying_quote,
            current_balances=ctx.current_balances,
        )
        return RuleResult()


class EmitPanelRule:
    """Write the rendered panel to stderr."""

    name = "emit_panel"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.panel_text is not None and not ctx.panel_emitted

    def execute(self, ctx: OrderContext) -> RuleResult:
        typer.echo(ctx.panel_text, err=True)
        ctx.panel_emitted = True
        return RuleResult()


class PolicyEvaluateRule:
    """Run the policy engine and render the Policy Check section.

    Skipped when no profile loaded (preview-only path); the panel
    already showed a "(no profile loaded — gate skipped)" hint via the
    no-profile branch in ``DryRunCompleteRule``.
    """

    name = "policy_evaluate"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.profile is not None

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import (
            _audit, _build_policy_context, _render_policy_decision,
        )
        from schwab_cli.order_policy import evaluate as policy_evaluate
        order_ctx = _build_policy_context(
            client=ctx.client, body=ctx.body, account=ctx.account,
            prof=ctx.profile,
            preview_raw=ctx.raw_preview if not ctx.preview_unavailable else None,
            sub=ctx.sub,
        )
        decision = policy_evaluate(ctx.profile, order_ctx)
        ctx.policy_decision = decision
        _audit(
            ctx.sub,
            "policy_evaluated" if decision.approved else "policy_rejected",
            account=ctx.account.account_number,
            profile_name=ctx.profile.name,
            decision=decision.decision,
            rule=decision.rule_name,
            phase=decision.rule_phase,
            reason=decision.reason,
            policy_evaluations=[
                {
                    "policy": ev.name,
                    "matched": ev.matched,
                    "effect": ev.effect,
                    "satisfied": ev.satisfied if ev.matched else None,
                    "conditions": [
                        {
                            "field": p.field, "op": p.op,
                            "expected": p.expected, "actual": p.actual,
                            "satisfied": p.satisfied,
                            "unevaluatable": p.unevaluatable,
                        }
                        for p in ev.predicates
                    ],
                }
                for ev in decision.evaluations
            ],
        )
        # Render the Policy Check section (only after the panel emitted).
        _render_policy_decision(decision)
        for warning in ctx.limits.warnings:
            typer.secho(
                f"limit warning [{warning.rule_name}]: {warning.message}",
                fg=typer.colors.YELLOW, err=True,
            )
        return RuleResult()


class NoProfileNoticeRule:
    """When no profile loaded, print the "(gate skipped)" banner."""

    name = "no_profile_notice"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.profile is None and ctx.panel_emitted

    def execute(self, ctx: OrderContext) -> RuleResult:
        typer.secho(
            "\nPolicy Check  (no profile loaded — gate skipped)",
            fg=typer.colors.YELLOW, err=True,
        )
        for warning in ctx.limits.warnings:
            typer.secho(
                f"limit warning [{warning.rule_name}]: {warning.message}",
                fg=typer.colors.YELLOW, err=True,
            )
        return RuleResult()


class DryRunCompleteRule:
    """Dry-run terminus. Audits + prints final blurb + halts cleanly."""

    name = "dry_run_complete"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _audit
        if ctx.profile is None:
            _audit(ctx.sub, "dry_run_done",
                   account=ctx.account.account_number, profile_name=None)
            if ctx.as_json:
                typer.echo(_json.dumps(
                    {"order": ctx.body,
                     "preview": ctx.preview_summary.__dict__
                                 if ctx.preview_summary else None},
                    default=str,
                ))
            else:
                typer.echo(
                    "(dry-run: no profile evaluated; not sending placeOrder)",
                    err=True,
                )
            return RuleResult(halt=True, exit_code=0)

        _audit(ctx.sub, "dry_run_done", account=ctx.account.account_number)
        if ctx.as_json:
            typer.echo(_json.dumps(
                {"order": ctx.body,
                 "preview": ctx.preview_summary.__dict__
                            if ctx.preview_summary else None},
                default=str,
            ))
        else:
            decision = ctx.policy_decision
            if decision is None or decision.approved:
                typer.echo("(dry-run: not sending placeOrder)", err=True)
            else:
                typer.secho(
                    "WARNING: this order would be REJECTED by policy "
                    f"{ctx.profile.name!r} during a real `order place`. "
                    "Preview exits 0 anyway — informational only.",
                    fg=typer.colors.YELLOW, err=True,
                )
        return RuleResult(halt=True, exit_code=0)


class PolicyDenyGateRule:
    """Real-place hard gate on policy deny.

    Bypassed only when ``--override`` is in flight (the override path
    handles its own ceremony).
    """

    name = "policy_deny_gate"

    def applies(self, ctx: OrderContext) -> bool:
        return (
            not ctx.dry_run
            and not ctx.overriding
            and ctx.policy_decision is not None
        )

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import EXIT_POLICY_REJECTED
        decision = ctx.policy_decision
        if decision.approved:
            return RuleResult()
        typer.secho(
            f"REJECTED by policy {ctx.profile.name!r}: {decision.reason}",
            fg=typer.colors.RED, err=True,
        )
        typer.secho(
            "(no order sent to Schwab. Use `--profile <other>` or pass "
            "`--override REASON --override-confirm` to bypass.)",
            fg=typer.colors.RED, err=True,
        )
        return RuleResult(halt=True, exit_code=EXIT_POLICY_REJECTED)


class PreviewRejectGateRule:
    """Real-place hard gate on Schwab's preview rejects.

    Per spec, ``--override`` only bypasses the policy gate, NOT this
    one. Schwab's reject means the order won't fill anyway.
    """

    name = "preview_reject_gate"

    def applies(self, ctx: OrderContext) -> bool:
        return (
            not ctx.dry_run
            and ctx.preview_summary is not None
            and bool(ctx.preview_summary.rejects)
        )

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _audit, EXIT_REJECTED
        for r in ctx.preview_summary.rejects:
            typer.secho(f"Schwab preview rejected: {r}",
                        fg=typer.colors.RED, err=True)
        typer.secho(
            "(no order sent. Schwab would refuse the order during placeOrder.)",
            fg=typer.colors.RED, err=True,
        )
        _audit(
            ctx.sub, "preview_rejected_pre_place",
            account=ctx.account.account_number,
            rejects=list(ctx.preview_summary.rejects),
        )
        return RuleResult(halt=True, exit_code=EXIT_REJECTED)


class OverrideCeremonyRule:
    """Run the override-typed-prompt + notification ceremony.

    Replaces the policy-deny gate when ``--override`` is given.
    """

    name = "override_ceremony"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.overriding and not ctx.dry_run

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _run_override_path
        _run_override_path(
            body=ctx.body, account=ctx.account, prof=ctx.profile,
            override_reason=ctx.override_reason or "", sub=ctx.sub,
        )
        return RuleResult()


class ConfirmRule:
    """Final yes/no prompt. Always runs for real-place; ``--yes`` is
    handled inside ``_confirm_or_abort`` (prints "skipping" and returns).

    For interactive (non-``--yes``) place runs we also start a
    :class:`LiveTicker` that polls the underlying quote in the
    background and repaints a status line above the prompt every ~1.5s,
    so the operator decides on fresh data."""

    name = "confirm"

    def applies(self, ctx: OrderContext) -> bool:
        return not ctx.dry_run

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import (
            _audit, _confirm_or_abort, _fetch_underlying_quote_safe,
        )

        # Only run a live ticker when (a) we'll actually display a prompt
        # and (b) we have an underlying symbol from the panel-time fetch.
        skip_prompt = ctx.yes and not ctx.overriding
        ticker = None
        if not skip_prompt:
            # Blank-line separator between panel and prompt; emitted here
            # (not inside _confirm_or_abort) so the ticker's initial line
            # lands directly above the prompt for deterministic repaint.
            typer.echo("", err=True)
            if ctx.underlying_quote is not None:
                from .live_ticker import LiveTicker
                initial = _format_live_line(ctx.underlying_quote)
                ticker = LiveTicker(
                    fetch=lambda: _fetch_underlying_quote_safe(
                        ctx.client, ctx.body,
                    ),
                    render=_format_live_line,
                    initial_line=initial,
                )
                ticker.start()

        try:
            try:
                # `--override` paths have their own ceremony already; the
                # standard --yes / yes prompt still runs after.
                _confirm_or_abort(yes=ctx.yes if not ctx.overriding else False)
            except typer.Exit as exit_:
                if int(exit_.exit_code or 0) == 0:
                    _audit(
                        ctx.sub, "aborted",
                        account=ctx.account.account_number,
                        **({"override": "aborted_at_yes"} if ctx.overriding else {}),
                    )
                raise
            _audit(
                ctx.sub, "confirmed",
                account=ctx.account.account_number,
                via="--yes" if (ctx.yes and not ctx.overriding) else "yes",
                **({"override": True} if ctx.overriding else {}),
            )
        finally:
            if ticker is not None:
                ticker.stop()
        return RuleResult()


def _format_live_line(q: dict) -> str:
    """One-line status: "Live <SYM>  $last  bid $bx ×N  ask $ax ×N  vol".

    Stays under 78 chars on typical terminals.
    """
    sym = q.get("symbol", "?")
    last = q.get("last")
    bid = q.get("bid")
    ask = q.get("ask")
    bid_size = q.get("bid_size")
    ask_size = q.get("ask_size")
    net_change = q.get("net_change")

    def _money(v: float | None) -> str:
        if v is None:
            return "—"
        return f"${v:,.2f}"

    def _signed(v: float | None) -> str:
        if v is None:
            return ""
        sign = "+" if v >= 0 else "-"
        return f"  ({sign}${abs(v):,.2f})"

    def _qty(v: float | int | None) -> str:
        if v is None:
            return "—"
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return "—"

    return (
        f"  Live {sym}  {_money(last)}{_signed(net_change)}  "
        f"bid {_money(bid)} ×{_qty(bid_size)}  "
        f"ask {_money(ask)} ×{_qty(ask_size)}"
    )


class PlaceOrderRule:
    """Submit the order to Schwab + bump counters + emit final output."""

    name = "place_order"

    def applies(self, ctx: OrderContext) -> bool:
        return not ctx.dry_run

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.api.client import ApiError, SessionExpired
        from schwab_cli.commands.order import (
            _audit, _handle_api_error, _safe_place, _underlying_from_body,
            EXIT_NETWORK, EXIT_REJECTED,
        )
        try:
            order_id, _resp = _safe_place(
                ctx.client, ctx.account, ctx.body, audit_subcommand=ctx.sub,
            )
        except ApiError as e:
            msg = str(e)
            is_4xx = msg.startswith(("400", "401", "403", "404", "409"))
            stage = (
                "rejected" if (is_4xx and "auth" not in msg.lower())
                else "place_failed"
            )
            _audit(
                ctx.sub, stage,
                account=ctx.account.account_number, error=msg,
            )
            if stage == "rejected":
                _handle_api_error(e, code=EXIT_REJECTED)
            else:
                _handle_api_error(e, code=EXIT_NETWORK)
            return RuleResult(halt=True, exit_code=EXIT_NETWORK)  # unreachable
        except SessionExpired as e:
            _audit(ctx.sub, "place_failed",
                   account=ctx.account.account_number, error=str(e))
            _handle_api_error(e, code=EXIT_NETWORK)
            return RuleResult(halt=True, exit_code=EXIT_NETWORK)  # unreachable

        ctx.placed_order_id = order_id
        _audit(ctx.sub, "placed",
               account=ctx.account.account_number, order_id=order_id)

        # Phase 2c counter bump (best-effort).
        try:
            from schwab_cli.order_policy import counters as _counters_mod
            _counters_mod.record_place(
                account_number=ctx.account.account_number,
                underlying=_underlying_from_body(ctx.body),
            )
        except Exception as e:  # noqa: BLE001
            _audit(
                ctx.sub, "counter_increment_failed",
                account=ctx.account.account_number, order_id=order_id,
                error=f"{type(e).__name__}: {e}",
            )

        if ctx.as_json:
            typer.echo(_json.dumps({"orderId": order_id}))
        else:
            typer.echo(f"Schwab: placed order {order_id}", err=True)
        return RuleResult()


# ---- canonical pipeline -------------------------------------------------


DEFAULT_RULES: tuple = (
    LoadProfileRule(),
    FetchAccountBalancesRule(),
    FetchUnderlyingQuoteRule(),
    SchwabPreviewRule(),
    ComputeAnalyticsRule(),
    RenderPanelRule(),
    EmitPanelRule(),
    PolicyEvaluateRule(),
    NoProfileNoticeRule(),
    DryRunCompleteRule(),
    OverrideCeremonyRule(),
    PolicyDenyGateRule(),
    PreviewRejectGateRule(),
    ConfirmRule(),
    PlaceOrderRule(),
)
