"""``schwab_cli policy ...`` subcommand handlers.

Phase 2a CLI surface:

* ``policy show [--profile NAME]`` — print the resolved profile in JSON.
* ``policy lint [--profile NAME] [--all]`` — validate one (or every)
  profile file. Exit 0 on clean, 2 on any error.
* ``policy test --order ORDER_JSON [--profile NAME] [--account ACC]``
  — dry-run evaluate one order body against a profile and print
  the decision + per-policy trace.

Profile selection priority:
  1. ``--profile NAME`` flag
  2. ``SCHWAB_CLI_PROFILE`` env var
  3. filename ``default``
"""

from __future__ import annotations

import json as _json
import os
import sys
from datetime import date
from pathlib import Path

import typer

from schwab_cli.order_policy import (
    PolicyConfigError,
    list_profiles,
    load_profile,
    profiles_dir,
)
from schwab_cli.order_policy.decision import evaluate
from schwab_cli.order_policy.fields import OrderContext
from schwab_cli.order_policy.loader import select_profile_name


def _resolve_name(flag: str | None) -> str:
    return select_profile_name(
        flag=flag, env=os.environ.get("SCHWAB_CLI_PROFILE"),
    )


def run_show(*, profile: str | None) -> None:
    name = _resolve_name(profile)
    try:
        prof = load_profile(name)
    except PolicyConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.echo(_json.dumps(_profile_to_json(prof), indent=2, default=str))


def run_lint(*, profile: str | None, all_profiles: bool) -> None:
    if all_profiles:
        names = list_profiles()
        if not names:
            typer.secho("no profiles found", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=0)
    else:
        names = [_resolve_name(profile)]

    errors = 0
    for n in names:
        try:
            load_profile(n)
            typer.echo(f"  ✓ {n}")
        except PolicyConfigError as e:
            errors += 1
            typer.secho(f"  ✗ {n}: {e}", fg=typer.colors.RED)
    if errors:
        typer.secho(
            f"\n{errors} profile(s) failed lint", fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)
    typer.secho(f"\nall {len(names)} profile(s) ok", fg=typer.colors.GREEN)


def run_test(
    *, order_json_path: str, profile: str | None,
    account: str | None,
) -> None:
    """Dry-run evaluate one order body against the resolved profile.

    ``order_json_path`` is a path to a JSON file containing the
    Schwab order body (the same shape we POST to /accounts/.../orders).
    Use `-` to read from stdin.
    """
    name = _resolve_name(profile)
    try:
        prof = load_profile(name)
    except PolicyConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if order_json_path == "-":
        body = _json.load(sys.stdin)
    else:
        with open(Path(order_json_path).expanduser(), "r", encoding="utf-8") as f:
            body = _json.load(f)
    if not isinstance(body, dict):
        typer.secho(
            "order JSON must be an object (the Schwab order body shape)",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    ctx = OrderContext(
        body=body,
        account_number=account or body.get("accountNumber") or "00000000",
        today=date.today(),
    )
    decision = evaluate(prof, ctx)

    out = {
        "profile": prof.name,
        "decision": decision.decision,
        "rule": decision.rule_name,
        "phase": decision.rule_phase,
        "reason": decision.reason,
        "evaluations": [
            {
                "policy": ev.name,
                "matched": ev.matched,
                "effect": ev.effect,
                "satisfied": ev.satisfied if ev.matched else None,
                "predicates": [
                    {
                        "field": p.field, "op": p.op,
                        "expected": p.expected, "actual": p.actual,
                        "satisfied": p.satisfied,
                        "unevaluatable": p.unevaluatable,
                        "error": p.error,
                    }
                    for p in ev.predicates
                ],
            }
            for ev in decision.evaluations
        ],
    }
    typer.echo(_json.dumps(out, default=str, indent=2))
    if not decision.approved:
        raise typer.Exit(code=4)


# ---- helpers --------------------------------------------------------------


def _profile_to_json(p) -> dict:
    return {
        "name": p.name,
        "description": p.description,
        "default_action": p.default_action,
        "allow_override": p.allow_override,
        "override_confirmation": p.override_confirmation,
        "override_max_per_day": p.override_max_per_day,
        "notify_on_override": p.notify_on_override,
        "policies": [
            {
                "name": pp.name,
                "description": pp.description,
                "enabled": pp.enabled,
                "match": _match_to_json(pp.match),
                "conditions": [_cond_to_json(c) for c in pp.conditions],
                "effect": pp.effect,
                "reason": pp.reason,
                "tags": list(pp.tags),
            }
            for pp in p.policies
        ],
    }


def _match_to_json(m):
    from schwab_cli.order_policy.schema import (
        AllOfMatch, AnyOfMatch, FieldMatch, WildcardMatch,
    )
    if isinstance(m, WildcardMatch):
        return "*"
    if isinstance(m, FieldMatch):
        out = {k: list(v) for k, v in m.fields.items()}
        for k, v in m.negated_fields.items():
            out[f"not_{k}"] = list(v)
        return out
    if isinstance(m, AnyOfMatch):
        return {"any_of": [_match_to_json(c) for c in m.clauses]}
    if isinstance(m, AllOfMatch):
        return {"all_of": [_match_to_json(c) for c in m.clauses]}
    return None


def _cond_to_json(c):
    from schwab_cli.order_policy.schema import (
        AndCondition, NotCondition, OrCondition, Predicate,
    )
    if isinstance(c, Predicate):
        return {c.field_name: dict(c.op_values)}
    if isinstance(c, AndCondition):
        return {"and": [_cond_to_json(x) for x in c.children]}
    if isinstance(c, OrCondition):
        return {"or": [_cond_to_json(x) for x in c.children]}
    if isinstance(c, NotCondition):
        return {"not": [_cond_to_json(x) for x in c.children]}
    return None


def _ensure_profiles_dir_exists() -> Path:
    """Best-effort create the profiles dir (so it shows up in `ls`)."""
    base = profiles_dir()
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base
