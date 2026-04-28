"""Pipeline executor. Iterates a list of rules over an ``OrderContext``."""

from __future__ import annotations

import os
import sys
import time
from typing import Iterable

from .context import OrderContext, RuleResult


class PipelineExit(Exception):
    """Raised when a rule asks the pipeline to halt with an exit code."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"pipeline exit {exit_code}")
        self.exit_code = exit_code


def run_pipeline(rules: Iterable, ctx: OrderContext) -> None:
    """Run each rule in order against ``ctx``.

    A rule with ``applies(ctx) == False`` is skipped. A rule whose
    ``execute(ctx)`` returns ``halt=True`` stops the pipeline and
    raises ``PipelineExit(exit_code)``.

    Set ``SCHWAB_CLI_PROFILE_PIPELINE=1`` to print per-rule wall-clock
    timing to stderr. Useful when ``order place`` feels slow — most
    of the latency is Schwab REST round-trips (account, quote, chain,
    previewOrder) which run serially today.
    """
    profile = os.environ.get("SCHWAB_CLI_PROFILE_PIPELINE") in ("1", "true", "yes")
    rules_list = list(rules)  # may be a generator; we want to iterate twice
    # Each entry: (name, dt, skipped, child_timings).
    # ``child_timings`` is non-empty only for wrapper rules that fan
    # out (currently ParallelFetchRule).
    timings: list[tuple[str, float, bool, list[tuple[str, float]]]] = []
    total_start = time.perf_counter()
    for rule in rules_list:
        name = getattr(rule, "name", type(rule).__name__)
        if not rule.applies(ctx):
            if profile:
                timings.append((name, 0.0, True, []))
            continue
        t0 = time.perf_counter()
        try:
            result: RuleResult = rule.execute(ctx)
        finally:
            if profile:
                children = list(getattr(rule, "last_child_timings", []) or [])
                timings.append((name, time.perf_counter() - t0, False, children))
        if result.halt:
            if profile:
                _emit_timings(timings, time.perf_counter() - total_start)
            raise PipelineExit(result.exit_code)
    if profile:
        _emit_timings(timings, time.perf_counter() - total_start)


def _emit_timings(
    timings: list[tuple[str, float, bool, list[tuple[str, float]]]],
    total: float,
) -> None:
    """Tab-aligned per-rule timing report on stderr.

    Wrapper rules (e.g. ``ParallelFetchRule``) print one row for the
    wrapper's wall-clock plus indented rows for each child showing the
    parallel-arm time — so the report shows both "we waited 600ms" and
    "those 600ms were spent on three concurrent ~600ms calls".
    """
    width = max((len(n) for n, _, _, _ in timings), default=20)
    # Children are indented by 4 spaces; pad their column accordingly.
    child_width = max(
        (len(c[0]) for _, _, _, kids in timings for c in kids),
        default=0,
    )
    print("=== pipeline timing ===", file=sys.stderr)
    for name, dt, skipped, children in timings:
        if skipped:
            print(f"  {name:<{width}}  (skipped)", file=sys.stderr)
        else:
            print(f"  {name:<{width}}  {dt * 1000:7.1f} ms", file=sys.stderr)
        for child_name, child_dt in children:
            print(
                f"      └ {child_name:<{child_width}}  {child_dt * 1000:7.1f} ms",
                file=sys.stderr,
            )
    print(f"  {'TOTAL':<{width}}  {total * 1000:7.1f} ms", file=sys.stderr)
