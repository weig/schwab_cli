"""Pipeline executor. Iterates a list of rules over an ``OrderContext``."""

from __future__ import annotations

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
    """
    for rule in rules:
        if not rule.applies(ctx):
            continue
        result: RuleResult = rule.execute(ctx)
        if result.halt:
            raise PipelineExit(result.exit_code)
