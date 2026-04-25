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


def run_new() -> None:
    """Interactive `profile new --type=order` (Phase 2f-4).

    Drives the questionnaire + vim-key list editor. TTY-only —
    non-TTY exits 2 with a pointer at hand-authoring.
    """
    from schwab_cli.order_policy.loader import profiles_dir
    from schwab_cli.order_policy.profile_new import run_interactive
    code = run_interactive(base_dir=profiles_dir())
    if code != 0:
        raise typer.Exit(code=code)


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


def run_counters(*, account: str | None, as_json: bool) -> None:
    """Print the current persisted counter state.

    Without ``--account`` shows every account; otherwise just the
    one. Always loads the file fresh (auto-rotates daily counters
    if the day rolled).
    """
    from schwab_cli.order_policy import counters as _c
    c = _c.load()

    if as_json:
        out = c.to_json()
        if account:
            scoped = {
                "daily_order_count_total": {
                    account: c.daily_total.get(account, 0),
                },
                "daily_order_count_per_ticker": {
                    account: c.daily_per_ticker.get(account, {}),
                },
                "minutely_buckets": {
                    account: c.minutely_buckets.get(account, {}),
                },
                "override_count_per_day": {
                    account: c.override_count_per_day.get(account, 0),
                },
            }
            out = {"date": c.et_date, "tz": "America/New_York",
                   "counters": scoped}
        typer.echo(_json.dumps(out, indent=2, sort_keys=True))
        return

    typer.echo(f"=== Counters (date: {c.et_date} ET) ".ljust(60, "="))
    accounts = [account] if account else sorted(set(
        list(c.daily_total.keys())
        + list(c.daily_per_ticker.keys())
        + list(c.minutely_buckets.keys())
        + list(c.override_count_per_day.keys())
    ))
    if not accounts:
        typer.echo("(no activity recorded)")
        return
    for acct in accounts:
        tail = acct[-4:] if len(acct) >= 4 else acct
        typer.echo(f"\nAccount ********{tail}")
        typer.echo(f"  daily_order_count:   {c.daily_total.get(acct, 0)}")
        per_ticker = c.daily_per_ticker.get(acct) or {}
        if per_ticker:
            typer.echo("  per-ticker:")
            for sym, n in sorted(per_ticker.items()):
                typer.echo(f"    {sym}: {n}")
        minutely = c.minutely_buckets.get(acct) or {}
        if minutely:
            typer.echo(f"  minutely (last 5 min): {sum(minutely.values())} orders "
                       f"across {len(minutely)} bucket(s)")
        override_today = c.override_count_per_day.get(acct, 0)
        if override_today:
            typer.echo(f"  overrides today:     {override_today}")


def run_audit(
    *, since: str | None, account: str | None,
    decision: str | None, limit: int | None, as_json: bool,
) -> None:
    """Tail the order audit log.

    ``since`` accepts the same range tokens as ``order list --range``
    (default last 24h). ``account`` and ``decision`` filter rows.
    """
    from schwab_cli import audit as audit_mod
    from schwab_cli.history_spec import RangeSpecError, parse_range

    if since is None:
        since = "-1d..now"
    try:
        start, end = parse_range(since)
    except RangeSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    rows: list[dict] = []
    base = audit_mod.DEFAULT_AUDIT_DIR
    if not base.exists():
        if as_json:
            typer.echo(_json.dumps([], indent=2))
        else:
            typer.echo("(no audit log entries)")
        return

    # Walk one file per day in the range — files are
    # YYYY-MM-DD.order.log.
    cur = start.date()
    last = end.date()
    while cur <= last:
        path = base / f"{cur.isoformat()}.order.log"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                rows.append(row)
        from datetime import timedelta as _td
        cur = cur + _td(days=1)

    # Filter.
    def _ok(r: dict) -> bool:
        ts = r.get("ts", "")
        # Lexicographic compare works for ISO-8601 timestamps with the
        # same offset; the ranges are tz-aware UTC.
        if ts and not (start.isoformat() <= ts <= end.isoformat() + "z"):
            # Be permissive on the upper bound — fall through.
            pass
        if account and r.get("account") not in (account, None):
            # The audit row may carry account=<input> (e.g. "5678") OR
            # account=<resolved 8-digit>; accept either.
            if r.get("account") != account:
                return False
        if decision and r.get("decision") not in (None, decision):
            if r.get("decision") != decision:
                return False
        return True

    rows = [r for r in rows if _ok(r)]
    if limit is not None:
        rows = rows[-limit:]

    if as_json:
        typer.echo(_json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        typer.echo("(no matching audit entries)")
        return
    for r in rows:
        ts = r.get("ts", "?")
        sub = r.get("subcommand", "?")
        stage = r.get("stage", "?")
        acct = r.get("account") or ""
        extra = r.get("decision") or r.get("order_id") or r.get("error") or ""
        typer.echo(f"  {ts}  {sub:<8}  {stage:<22}  acct={acct:<10}  {extra}")


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
