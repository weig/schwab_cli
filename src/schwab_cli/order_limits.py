"""Per-order limit rules.

Phase 1 stub. The full rules engine ships in **Phase 3** alongside
the configurable rules file format. For now :func:`evaluate` always
returns the empty result so callers can wire the check in
unconditionally without behaviour changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Effect = Literal["forbid", "warn"]


@dataclass(frozen=True)
class LimitFinding:
    """One rule firing against the order under consideration."""

    effect: Effect
    rule_name: str
    message: str


@dataclass(frozen=True)
class LimitResult:
    """Aggregate result of evaluating all limit rules."""

    findings: tuple[LimitFinding, ...] = field(default_factory=tuple)

    @property
    def forbidden(self) -> bool:
        return any(f.effect == "forbid" for f in self.findings)

    @property
    def warnings(self) -> tuple[LimitFinding, ...]:
        return tuple(f for f in self.findings if f.effect == "warn")


def evaluate(body: dict, *, account_number: str) -> LimitResult:
    """Evaluate the order body against the configured limit rules.

    Phase 1: always returns an empty result. Phase 3 will load
    ``~/.config/schwab_cli/order_limits.json`` and apply rule logic.
    """
    return LimitResult()
