"""Interactive profile authoring (Phase 2f-4).

Public:

* :func:`run_interactive` — entry point invoked from the CLI handler.
  Drives the questionnaire + the vim-key list editor + atomic save.
* :class:`EditorState` and the pure mutation primitives in
  :mod:`state` — exercised in tests.
* :data:`TEMPLATES` — pre-canned policy shapes.
"""

from __future__ import annotations

from schwab_cli.order_policy.profile_new.editor import run_interactive
from schwab_cli.order_policy.profile_new.save import (
    ProfileExistsError,
    atomic_save,
)
from schwab_cli.order_policy.profile_new.state import (
    EditorState,
    add_policy,
    delete_at_cursor,
    mark_saved,
    move_down,
    move_up,
    render_list,
    undo_delete,
)
from schwab_cli.order_policy.profile_new.templates import (
    TEMPLATES,
    Prompter,
    Template,
    by_key,
)

__all__ = [
    "EditorState",
    "ProfileExistsError",
    "Prompter",
    "TEMPLATES",
    "Template",
    "add_policy",
    "atomic_save",
    "by_key",
    "delete_at_cursor",
    "mark_saved",
    "move_down",
    "move_up",
    "render_list",
    "run_interactive",
    "undo_delete",
]
