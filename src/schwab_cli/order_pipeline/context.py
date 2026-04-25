"""Pipeline context + per-rule result types.

``OrderContext`` is the single mutable bag passed through every rule.
Rules read inputs (spec, body, account, flags) and fill outputs
(profile, preview_summary, decision, etc.) for downstream rules.

``RuleResult`` is what each rule's ``execute`` returns: optional
``halt`` to stop the pipeline, with an exit code for the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderContext:
    """Shared pipeline state.

    Inputs are populated by the caller (run_place); outputs are
    progressively filled by rules in pipeline order.
    """

    # ---- inputs ---------------------------------------------------------
    spec: Any                    # _NormalizedOrder
    body: dict
    account: Any                 # AccountIds
    client: Any                  # SchwabClient
    sub: str                     # "preview" | "place" — drives audit subcommand
    dry_run: bool
    yes: bool
    overriding: bool
    profile_name: str
    override_reason: str | None
    as_json: bool
    limits: Any                  # order_limits.evaluate result

    # ---- outputs (filled by rules) -------------------------------------
    profile: Any | None = None
    profile_load_error: str | None = None
    underlying_quote: dict | None = None
    current_balances: dict | None = None
    account_positions: list | None = None
    preview_summary: Any | None = None
    preview_unavailable: bool = False
    raw_preview: dict | None = None
    analytics: Any | None = None
    panel_text: str | None = None
    panel_emitted: bool = False
    policy_decision: Any | None = None
    placed_order_id: str | None = None

    # Free-form tags rules can set / inspect (e.g. "confirmed_via=yes").
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleResult:
    """What a rule's ``execute`` returns.

    * ``halt=False`` (default) — pipeline advances to the next rule.
    * ``halt=True`` — pipeline stops; ``exit_code`` is the CLI exit.
      The runner raises ``PipelineExit(exit_code)`` so the caller can
      let typer/Click handle it.
    """

    halt: bool = False
    exit_code: int = 0
