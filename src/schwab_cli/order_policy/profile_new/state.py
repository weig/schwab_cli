"""Pure state machine for the policy list editor.

No prompt_toolkit, no I/O. The editor.py driver renders + binds keys
to these mutations; tests exercise them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _UndoEntry:
    """One deletion popped onto the undo stack."""
    policy: dict
    original_index: int


@dataclass
class EditorState:
    """The mutable list-editor state.

    Attributes:
        policies: ordered list of policy dicts (Schwab order JSON shape).
        cursor: 0-indexed row the cursor is on (clamped to valid range).
        undo_stack: deletions waiting for `u` to restore.
        dirty: True if any mutation has happened since the last save.
        saved_path: filesystem path the last successful save wrote to,
            or None if not yet saved.
    """

    policies: list[dict] = field(default_factory=list)
    cursor: int = 0
    undo_stack: list[_UndoEntry] = field(default_factory=list)
    dirty: bool = False
    saved_path: str | None = None

    @property
    def empty(self) -> bool:
        return not self.policies


# ---- mutation primitives -------------------------------------------------


def move_down(s: EditorState) -> None:
    """j / Down. No-op on empty list or last row."""
    if not s.policies:
        return
    if s.cursor < len(s.policies) - 1:
        s.cursor += 1


def move_up(s: EditorState) -> None:
    """k / Up. No-op on empty list or first row."""
    if not s.policies:
        return
    if s.cursor > 0:
        s.cursor -= 1


def add_policy(s: EditorState, policy: dict) -> None:
    """Append after the cursor (or at end if list was empty).

    The new row becomes the cursor target so the user sees what they
    just created. Marks dirty.
    """
    insert_at = len(s.policies) if s.empty else s.cursor + 1
    s.policies.insert(insert_at, policy)
    s.cursor = insert_at
    s.dirty = True


def delete_at_cursor(s: EditorState) -> bool:
    """d. Pop the policy under the cursor onto the undo stack and
    remove it. Returns True if something was deleted; False on empty
    list. Cursor moves up one if it would otherwise fall off the end.
    """
    if not s.policies:
        return False
    idx = s.cursor
    popped = s.policies.pop(idx)
    s.undo_stack.append(_UndoEntry(policy=popped, original_index=idx))
    if s.cursor >= len(s.policies):
        s.cursor = max(0, len(s.policies) - 1)
    s.dirty = True
    return True


def undo_delete(s: EditorState) -> bool:
    """u. Pop the top of the undo stack and re-insert at its original
    index (clamped to end-of-list). Returns True on success, False if
    stack is empty.
    """
    if not s.undo_stack:
        return False
    entry = s.undo_stack.pop()
    insert_at = min(entry.original_index, len(s.policies))
    s.policies.insert(insert_at, entry.policy)
    s.cursor = insert_at
    s.dirty = True
    return True


def mark_saved(s: EditorState, path: str) -> None:
    """Called after a successful save. Clears the dirty flag and
    records the path."""
    s.dirty = False
    s.saved_path = path


# ---- pretty render helpers (used by editor + tests) ---------------------


def render_list(s: EditorState) -> str:
    """Plain-text render of the current state — used by the
    prompt_toolkit driver and exercised in tests as a stable
    representation of the visual."""
    if not s.policies:
        return "  (no policies — press `c` to add one)"
    lines = []
    for i, p in enumerate(s.policies):
        marker = ">" if i == s.cursor else " "
        effect = p.get("effect", "?")
        name = p.get("name", "?")
        match_summary = _summarise_match(p.get("match"))
        lines.append(
            f"  {marker} [{i + 1}] {effect:<6} {name:<30}  match: {match_summary}"
        )
    return "\n".join(lines)


def _summarise_match(m: Any) -> str:
    """One-line, human-readable form of a match clause for the
    editor's right-hand column. Best-effort."""
    if m == "*" or m == {} or m is None:
        return "*"
    if isinstance(m, dict):
        if "any_of" in m or "all_of" in m:
            return "(compound)"
        parts = []
        for k, v in m.items():
            if isinstance(v, list):
                parts.append(f"{k}={','.join(str(x) for x in v)}")
            else:
                parts.append(f"{k}={v}")
        return " ".join(parts) or "*"
    return str(m)
