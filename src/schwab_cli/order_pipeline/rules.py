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

    Always runs for preview and interactive place (panel review needs
    BP + position data). For ``place --yes`` the operator already
    committed and won't see the panel, so we skip the fetch unless the
    profile actually references account-derived fields (e.g. a policy
    that gates on buyingPower or position count).

    Stashes the positions list as well so :class:`DetectOpenCloseRule`
    can map option legs to existing positions without re-fetching.
    """

    name = "fetch_account"

    def applies(self, ctx: OrderContext) -> bool:
        if ctx.dry_run or not ctx.yes:
            return True
        # place --yes: only fetch when the policy needs account data.
        if ctx.profile is None:
            return False
        from schwab_cli.order_policy.sources import (
            referenced_fields, required_sources,
        )
        try:
            needed = required_sources(referenced_fields(ctx.profile))
        except Exception:  # noqa: BLE001 — defensive
            return False
        return "account" in needed

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _fetch_account_safe
        try:
            raw = _fetch_account_safe(
                ctx.client, ctx.account.account_number,
            )
        except Exception:  # noqa: BLE001 — best-effort
            return RuleResult()
        if not isinstance(raw, dict):
            return RuleResult()
        sec = raw.get("securitiesAccount") or {}
        cur = sec.get("currentBalances") or {}
        if isinstance(cur, dict):
            ctx.current_balances = {
                "stockBuyingPower": cur.get("buyingPower"),
                "optionBuyingPower": cur.get("availableFunds"),
            }
        positions = sec.get("positions")
        ctx.account_positions = positions if isinstance(positions, list) else []
        return RuleResult()


class DetectOpenCloseRule:
    """Rewrite option legs to ``*_TO_CLOSE`` when a matching position
    already exists.

    User typed ``BUY +1 AMZN ... PUT`` against an existing short put →
    real intent is ``BUY_TO_CLOSE`` (closing the short), not
    ``BUY_TO_OPEN``. TOS auto-promotes; we should too. Sending the
    wrong instruction also makes Schwab's previewOrder return BP
    projections that don't reflect the close, so this rewrite has to
    run BEFORE :class:`SchwabPreviewRule`.

    Sets ``leg.positionEffect = "CLOSING"`` to match Schwab's wire
    convention. Audits each rewrite so the change is visible in the
    log.
    """

    name = "detect_open_close"

    def applies(self, ctx: OrderContext) -> bool:
        # Need positions; only runs when the account fetch ran.
        return ctx.account_positions is not None and any(
            (leg.get("instrument") or {}).get("assetType") == "OPTION"
            for leg in ctx.body.get("orderLegCollection") or []
        )

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _audit
        positions = ctx.account_positions or []
        rewrites: list[dict] = []
        for leg in ctx.body.get("orderLegCollection") or []:
            inst = leg.get("instrument") or {}
            if inst.get("assetType") != "OPTION":
                continue
            # Honor user-stated effect: if ``positionEffect`` is already
            # set on the leg, the operator wrote ``[TO OPEN]`` /
            # ``[TO CLOSE]`` (or the ``--leg ...c`` suffix) — auto-detect
            # never overrides explicit intent.
            if leg.get("positionEffect"):
                continue
            sym = inst.get("symbol")
            pos = next(
                (p for p in positions
                 if (p.get("instrument") or {}).get("symbol") == sym),
                None,
            )
            if pos is None:
                continue
            short_qty = float(pos.get("shortQuantity") or 0)
            long_qty = float(pos.get("longQuantity") or 0)
            old = leg.get("instruction")
            new = old
            if old == "BUY_TO_OPEN" and short_qty > 0:
                new = "BUY_TO_CLOSE"
            elif old == "SELL_TO_OPEN" and long_qty > 0:
                new = "SELL_TO_CLOSE"
            if new != old:
                leg["instruction"] = new
                leg["positionEffect"] = "CLOSING"
                rewrites.append({"symbol": sym, "from": old, "to": new})
        if rewrites:
            _audit(
                ctx.sub, "leg_open_close_rewritten",
                account=ctx.account.account_number,
                rewrites=rewrites,
            )
        return RuleResult()


class FetchUnderlyingQuoteRule:
    """Step 2b — one-shot underlying quote for the Underlying section.

    Skipped on ``place --yes``. Real-place runs additionally start a
    LiveTicker around the confirm prompt that repaints a status line
    above it — see :class:`ConfirmRule`.
    """

    name = "fetch_underlying_quote"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.commands.order import _fetch_underlying_quote_safe
        ctx.underlying_quote = _fetch_underlying_quote_safe(ctx.client, ctx.body)
        return RuleResult()


class FetchChainRule:
    """Step 2c — option-chain pull for POP enrichment.

    Single-expiry option orders only — multi-expiry strategies
    (CALENDAR / DIAGONAL / etc.) need per-leg chain calls and a
    different POP shape; punt for now.

    Best-effort: chain endpoint failures leave ``ctx.chain_data``
    ``None`` and the analytics renderer prints "(unavailable)" rather
    than blocking the order.
    """

    name = "fetch_chain"

    def applies(self, ctx: OrderContext) -> bool:
        if not (ctx.dry_run or not ctx.yes):
            return False
        # Only meaningful for option orders. Equity has no chain.
        legs = (ctx.body or {}).get("orderLegCollection") or []
        return any(
            (l.get("instrument") or {}).get("assetType") == "OPTION"
            for l in legs
        )

    def execute(self, ctx: OrderContext) -> RuleResult:
        from datetime import date

        from schwab_cli.api.chains import get_chain
        from schwab_cli.output.chains import shape_envelope

        legs = (ctx.body or {}).get("orderLegCollection") or []
        # Collect distinct expiries from each option OSI symbol.
        expiries: set[date] = set()
        for leg in legs:
            sym = ((leg.get("instrument") or {}).get("symbol") or "")
            if len(sym) < 21:
                continue
            try:
                yymmdd = sym[6:12]
                expiries.add(date(
                    2000 + int(yymmdd[:2]),
                    int(yymmdd[2:4]),
                    int(yymmdd[4:6]),
                ))
            except (ValueError, IndexError):
                continue
        # Multi-expiry strategies skip POP for now (the existing
        # ``pop()`` accepts mixed expiries via dte=0 fallback, but
        # the result isn't meaningful without per-expiry chains).
        if len(expiries) != 1:
            return RuleResult()
        expiry = next(iter(expiries))
        try:
            raw = get_chain(
                ctx.client, ctx.spec.underlying.upper(),
                contract_type="ALL", strike_count=50,
                from_date=expiry, to_date=expiry,
            )
        except Exception:  # noqa: BLE001 — best-effort; POP just stays None
            return RuleResult()
        try:
            ctx.chain_data = shape_envelope(raw)
        except Exception:  # noqa: BLE001
            ctx.chain_data = None
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
            body=ctx.body,
            chain_data=ctx.chain_data,
        )
        return RuleResult()


class RenderPanelRule:
    """Build the confirmation panel string. Doesn't emit yet — that's the
    next rule's job — so the panel can be re-rendered with live ticks."""

    name = "render_panel"

    def applies(self, ctx: OrderContext) -> bool:
        return ctx.dry_run or not ctx.yes

    def execute(self, ctx: OrderContext) -> RuleResult:
        from schwab_cli.output.orders import (
            PreviewSummary, render_confirmation, render_order_ticket,
        )
        # Even when preview was skipped (shouldn't happen with current
        # applies() set, but be defensive), render with an empty summary.
        preview = ctx.preview_summary or PreviewSummary(
            None, None, None, None, (), (),
        )
        # Skip the Underlying section in the panel when the LiveTicker
        # will run — the ticker line above the prompt is the single
        # source of "this is updating right now". The panel section
        # would otherwise sit far above the prompt as a stale snapshot.
        ticker_will_run = (
            not ctx.dry_run and not ctx.yes
            and ctx.underlying_quote is not None
        )
        panel_underlying = None if ticker_will_run else ctx.underlying_quote
        # Schwab/TOS-style ticket string for copy/paste-back into Schwab
        # — best-effort, falls through to None for shapes we don't render.
        try:
            schwab_ticket = render_order_ticket(
                ctx.body, underlying=ctx.spec.underlying,
            )
        except Exception:  # noqa: BLE001 — never block the panel
            schwab_ticket = None
        ctx.panel_text = render_confirmation(
            body=ctx.body,
            account_tail=ctx.account.account_number[-4:],
            strategy_label=ctx.spec.strategy_label,
            is_naked_short=ctx.spec.is_naked_short,
            analytics=ctx.analytics,
            preview=preview,
            preview_unavailable=ctx.preview_unavailable,
            underlying_quote=panel_underlying,
            current_balances=ctx.current_balances,
            schwab_ticket=schwab_ticket,
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
    """One-line live status above the confirm prompt.

    Format::

        Live NVDA  $208.10  (-$0.31)  bid $208.05 ×1,500  ask $208.10 ×300  vol 12.3M

    Replaces the panel's Underlying section for real-place runs (the
    static section is hidden when the ticker is active), so this line
    must carry every datum the operator needs at decision time.
    """
    sym = q.get("symbol", "?")
    last = q.get("last")
    bid = q.get("bid")
    ask = q.get("ask")
    bid_size = q.get("bid_size")
    ask_size = q.get("ask_size")
    volume = q.get("volume")
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

    def _vol(v: float | int | None) -> str:
        if v is None:
            return "—"
        try:
            n = int(v)
        except (TypeError, ValueError):
            return "—"
        if n >= 1_000_000_000:
            return f"{n / 1e9:.2f}B"
        if n >= 1_000_000:
            return f"{n / 1e6:.2f}M"
        if n >= 1_000:
            return f"{n / 1e3:.1f}K"
        return f"{n}"

    # Anchored at column 0 with a leading 📡 icon so the live row is
    # visually distinct from the indented panel rows above it.
    return (
        f"📡 Live {sym}  {_money(last)}{_signed(net_change)}  "
        f"bid {_money(bid)} ×{_qty(bid_size)}  "
        f"ask {_money(ask)} ×{_qty(ask_size)}  "
        f"vol {_vol(volume)}"
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
    DetectOpenCloseRule(),       # must run BEFORE SchwabPreviewRule
    FetchUnderlyingQuoteRule(),
    FetchChainRule(),
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
