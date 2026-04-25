"""Decision algorithm — Phases A/B/C/D from spec §10.

Inputs: a fully-resolved :class:`Profile` and an :class:`OrderContext`
(plus an injectable :class:`FieldProvider`, defaulting to the Phase
2a one). Output: :class:`Decision` plus the per-policy evaluation
trace the audit log needs.

Algorithm:

1. Skip disabled policies.
2. **Phase A** — deny precedence: any matched-deny whose conditions
   are satisfied → REJECT.
3. **Phase B** — allow conditions must hold: any matched-allow whose
   conditions are NOT satisfied → REJECT.
4. **Phase C** — need-passing-allow: ≥1 matched-allow with conditions
   satisfied → APPROVE.
5. **Phase D** — fall through to ``profile.default_action``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from schwab_cli.order_policy.conditions import (
    PredicateResult,
    evaluate_conditions,
)
from schwab_cli.order_policy.fields import FieldProvider, OrderContext
from schwab_cli.order_policy.match import matches
from schwab_cli.order_policy.schema import Profile


@dataclass(frozen=True)
class PolicyEvaluation:
    """One policy's evaluation against an order. Goes into the audit
    log under ``policy_evaluations[]``."""

    name: str
    description: str
    enabled: bool
    matched: bool
    effect: str                          # "allow" | "deny"
    satisfied: bool                      # only meaningful when matched
    predicates: tuple[PredicateResult, ...]
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    decision: str                        # "approve" | "reject"
    profile_name: str
    rule_name: str | None                # which rule drove the decision
    rule_phase: str | None               # "A" | "B" | "C" | "D" | None
    reason: str                          # human-friendly summary
    failing_predicate: PredicateResult | None
    evaluations: tuple[PolicyEvaluation, ...]

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


def evaluate(
    profile: Profile,
    ctx: OrderContext,
    *,
    provider_factory: Callable[[OrderContext], FieldProvider] | None = None,
) -> Decision:
    """Run the full Phase A→D algorithm and return a :class:`Decision`.

    ``provider_factory`` is injectable so Phase 2b can swap in a richer
    provider with chain/account fetches without touching this module.
    """
    factory = provider_factory or FieldProvider
    provider = factory(ctx)
    cats = provider._cats  # categorical view, computed at provider init

    evaluations: list[PolicyEvaluation] = []
    for p in profile.policies:
        if not p.enabled:
            evaluations.append(PolicyEvaluation(
                name=p.name, description=p.description, enabled=False,
                matched=False, effect=p.effect, satisfied=False,
                predicates=(),
            ))
            continue
        m = matches(p.match, cats)
        if not m:
            evaluations.append(PolicyEvaluation(
                name=p.name, description=p.description, enabled=True,
                matched=False, effect=p.effect, satisfied=False,
                predicates=(),
            ))
            continue
        cond = evaluate_conditions(p.conditions, provider.get)
        evaluations.append(PolicyEvaluation(
            name=p.name, description=p.description, enabled=True,
            matched=True, effect=p.effect, satisfied=cond.satisfied,
            predicates=cond.predicates,
            reason=p.reason,
        ))

    # Phase A — deny precedence.
    for ev in evaluations:
        if ev.matched and ev.effect == "deny" and ev.satisfied:
            return Decision(
                decision="reject", profile_name=profile.name,
                rule_name=ev.name, rule_phase="A",
                reason=ev.reason or f"denied by policy {ev.name!r}",
                failing_predicate=None,
                evaluations=tuple(evaluations),
            )

    # Phase B — allow conditions must hold.
    for ev in evaluations:
        if ev.matched and ev.effect == "allow" and not ev.satisfied:
            failing = next(
                (p for p in ev.predicates if not p.satisfied), None,
            )
            return Decision(
                decision="reject", profile_name=profile.name,
                rule_name=ev.name, rule_phase="B",
                reason=(
                    f"allow policy {ev.name!r} matched but condition failed"
                    + (
                        f" ({_predicate_phrase(failing)})"
                        if failing is not None else ""
                    )
                ),
                failing_predicate=failing,
                evaluations=tuple(evaluations),
            )

    # Phase C — need ≥1 matched-allow with conditions satisfied.
    for ev in evaluations:
        if ev.matched and ev.effect == "allow" and ev.satisfied:
            return Decision(
                decision="approve", profile_name=profile.name,
                rule_name=ev.name, rule_phase="C",
                reason=f"approved by policy {ev.name!r}",
                failing_predicate=None,
                evaluations=tuple(evaluations),
            )

    # Phase D — default action.
    if profile.default_action == "allow":
        return Decision(
            decision="approve", profile_name=profile.name,
            rule_name=None, rule_phase="D",
            reason="approved by profile default_action=allow "
                   "(no policy matched)",
            failing_predicate=None,
            evaluations=tuple(evaluations),
        )
    return Decision(
        decision="reject", profile_name=profile.name,
        rule_name=None, rule_phase="D",
        reason="rejected by profile default_action=deny "
               "(no policy matched)",
        failing_predicate=None,
        evaluations=tuple(evaluations),
    )


def _predicate_phrase(p: PredicateResult) -> str:
    if p.error:
        return f"{p.field} {p.op}: {p.error}"
    if p.unevaluatable:
        return f"{p.field}: unavailable in current phase"
    return f"{p.field} {p.op} {p.expected!r} (actual={p.actual!r})"
