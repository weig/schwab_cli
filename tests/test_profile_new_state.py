"""Pure unit tests for the EditorState mutation primitives."""

from __future__ import annotations

from schwab_cli.order_policy.profile_new import (
    EditorState,
    add_policy,
    delete_at_cursor,
    move_down,
    move_up,
    render_list,
    undo_delete,
)


def _p(name: str, effect: str = "allow", match=None) -> dict:
    return {"name": name, "effect": effect, "match": match or "*"}


def test_initial_state_is_empty():
    s = EditorState()
    assert s.empty
    assert s.cursor == 0
    assert s.dirty is False


def test_move_on_empty_list_is_a_noop():
    s = EditorState()
    move_down(s); move_up(s)
    assert s.cursor == 0


def test_add_policy_advances_cursor_and_marks_dirty():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    assert [p["name"] for p in s.policies] == ["a", "b"]
    assert s.cursor == 1
    assert s.dirty is True


def test_add_policy_inserts_after_cursor_not_at_end():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("c"))
    assert [p["name"] for p in s.policies] == ["a", "c"]
    move_up(s)                      # cursor → 0 (a)
    add_policy(s, _p("b"))          # insert after a
    assert [p["name"] for p in s.policies] == ["a", "b", "c"]
    assert s.cursor == 1


def test_move_down_clamps_at_end():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    move_down(s); move_down(s)      # already at b; second is a no-op
    assert s.cursor == 1


def test_delete_at_cursor_pushes_to_undo_and_drops():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    add_policy(s, _p("c"))
    move_up(s)                      # cursor at b
    assert delete_at_cursor(s) is True
    assert [p["name"] for p in s.policies] == ["a", "c"]
    assert len(s.undo_stack) == 1
    assert s.undo_stack[-1].policy["name"] == "b"


def test_delete_clamps_cursor_when_was_last():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    # cursor is at index 1 (b)
    assert delete_at_cursor(s) is True
    assert [p["name"] for p in s.policies] == ["a"]
    assert s.cursor == 0


def test_delete_on_empty_returns_false():
    s = EditorState()
    assert delete_at_cursor(s) is False


def test_undo_restores_at_original_index():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    add_policy(s, _p("c"))
    move_up(s)                      # cursor at b
    delete_at_cursor(s)             # remove b
    assert [p["name"] for p in s.policies] == ["a", "c"]
    assert undo_delete(s) is True
    assert [p["name"] for p in s.policies] == ["a", "b", "c"]
    # cursor moves to the restored row.
    assert s.policies[s.cursor]["name"] == "b"


def test_undo_clamps_when_other_deletes_shrunk_list():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    add_policy(s, _p("c"))
    # Delete c first (cursor at index 2).
    delete_at_cursor(s)             # ['a', 'b'], cursor 1
    # Delete b (cursor at index 1).
    delete_at_cursor(s)             # ['a'], cursor 0
    # Now first undo restores 'b' at original index 1 → ['a', 'b'].
    assert undo_delete(s) is True
    assert [p["name"] for p in s.policies] == ["a", "b"]
    # Second undo restores 'c' at original index 2 → ['a', 'b', 'c'].
    assert undo_delete(s) is True
    assert [p["name"] for p in s.policies] == ["a", "b", "c"]


def test_undo_with_empty_stack_returns_false():
    s = EditorState()
    assert undo_delete(s) is False


def test_render_list_marks_cursor():
    s = EditorState()
    add_policy(s, _p("a"))
    add_policy(s, _p("b"))
    out = render_list(s)
    # Cursor is at index 1 after two adds.
    lines = out.splitlines()
    assert lines[0].startswith("   ")          # not the cursor row
    assert lines[1].startswith("  >")          # cursor row


def test_render_empty_state_hint():
    out = render_list(EditorState())
    assert "press `c`" in out
