"""Rule pipeline for ``schwab_cli order preview`` / ``order place``.

The same ordered list of :class:`OrderRule` objects drives both the
preview and the real-place flow. Each rule decides via ``applies()``
whether it runs in the current context (e.g. fetch-quote skips on
``--yes``); rules that block the pipeline (policy deny, Schwab
preview reject, dry-run done) return ``RuleResult(halt=True)`` so the
runner exits cleanly.

This keeps the order command a thin coordinator: validate args, build
the context, hand it to the runner.
"""

from __future__ import annotations

from .context import OrderContext, RuleResult
from .rules import DEFAULT_RULES, OrderRule
from .runner import PipelineExit, run_pipeline

# Alias for callers (e.g. commands/order.py) that already import the
# policy-engine's ``OrderContext`` from ``order_policy.fields`` and
# would otherwise have a name clash.
PipelineContext = OrderContext

__all__ = [
    "DEFAULT_RULES",
    "OrderContext",
    "OrderRule",
    "PipelineContext",
    "PipelineExit",
    "RuleResult",
    "run_pipeline",
]
