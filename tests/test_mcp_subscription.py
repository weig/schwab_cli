"""Tests for the MCP server's SubscriptionManager.

Pure logic, no I/O — verify refcount correctness, fan-out routing,
session-drop cleanup, and snapshot shape used by `mcp status`.
"""

from __future__ import annotations

import pytest

from schwab_cli.mcp_server.subscription import (
    SubKey,
    SubscriptionManager,
)


EQ = "LEVELONE_EQUITIES"


# ---- single-session basics --------------------------------------------


def test_add_subscription_returns_new_keys_on_first_add():
    m = SubscriptionManager()
    new = m.add("sess1", "tok1", EQ, ["NVDA", "AAPL"])
    assert new == {SubKey(EQ, "NVDA"), SubKey(EQ, "AAPL")}


def test_add_subscription_idempotent_for_second_caller():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    new = m.add("sess2", "tok2", EQ, ["NVDA"])
    # Second agent on same symbol: refcount goes 1->2, nothing new at Schwab.
    assert new == set()


def test_remove_subscription_returns_keys_to_unsubscribe():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    gone = m.remove("sess1", "tok1")
    assert gone == {SubKey(EQ, "NVDA")}


def test_remove_subscription_empty_when_others_still_subscribed():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    m.add("sess2", "tok2", EQ, ["NVDA"])
    gone = m.remove("sess1", "tok1")
    assert gone == set()


def test_fanout_targets_lists_all_session_tokens_for_symbol():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    m.add("sess2", "tok2", EQ, ["NVDA", "AAPL"])
    targets = m.fanout_targets(EQ, "NVDA")
    assert set(targets) == {("sess1", "tok1"), ("sess2", "tok2")}


def test_fanout_targets_only_matches_exact_service_symbol():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    m.add("sess2", "tok2", "LEVELONE_OPTIONS", ["NVDA260501C250"])
    assert m.fanout_targets(EQ, "NVDA") == [("sess1", "tok1")]
    assert m.fanout_targets("LEVELONE_OPTIONS", "NVDA260501C250") == [
        ("sess2", "tok2")
    ]


def test_fanout_targets_unknown_symbol_returns_empty():
    m = SubscriptionManager()
    assert m.fanout_targets(EQ, "NVDA") == []


# ---- session-drop cleanup ---------------------------------------------


def test_drop_session_unsubscribes_its_only_symbols():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA", "AAPL"])
    gone = m.drop_session("sess1")
    assert gone == {SubKey(EQ, "NVDA"), SubKey(EQ, "AAPL")}


def test_drop_session_leaves_overlapping_symbols_subscribed():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA", "AAPL"])
    m.add("sess2", "tok2", EQ, ["NVDA"])
    gone = m.drop_session("sess1")
    # NVDA still needed by sess2, so only AAPL goes.
    assert gone == {SubKey(EQ, "AAPL")}


def test_drop_session_removes_from_routing_index():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    m.add("sess2", "tok2", EQ, ["NVDA"])
    m.drop_session("sess1")
    # Only sess2 left.
    assert m.fanout_targets(EQ, "NVDA") == [("sess2", "tok2")]


def test_drop_session_unknown_session_is_noop():
    m = SubscriptionManager()
    assert m.drop_session("ghost") == set()


# ---- multiple tokens in one session ----------------------------------


def test_same_session_two_tokens_same_symbol_refcount_two():
    m = SubscriptionManager()
    m.add("sess1", "tok1", EQ, ["NVDA"])
    new = m.add("sess1", "tok2", EQ, ["NVDA"])
    assert new == set()
    # Remove the first tool call; second still needs NVDA.
    gone = m.remove("sess1", "tok1")
    assert gone == set()
    # Second removal → unsubscribe.
    gone2 = m.remove("sess1", "tok2")
    assert gone2 == {SubKey(EQ, "NVDA")}


# ---- active_symbols ---------------------------------------------------


def test_active_symbols_reflects_current_refcount():
    m = SubscriptionManager()
    assert m.active_symbols() == set()
    m.add("s1", "t1", EQ, ["NVDA", "AAPL"])
    assert m.active_symbols() == {SubKey(EQ, "NVDA"), SubKey(EQ, "AAPL")}
    m.remove("s1", "t1")
    assert m.active_symbols() == set()


# ---- snapshot ---------------------------------------------------------


def test_snapshot_shape():
    m = SubscriptionManager()
    m.add("s1", "t1", EQ, ["NVDA", "AAPL"])
    m.add("s2", "t2", EQ, ["NVDA"])
    snap = m.snapshot()

    assert snap["session_count"] == 2
    assert snap["subscription_count"] == 2  # NVDA, AAPL (unique keys)

    # Sessions block — per-session subscription list.
    sessions = snap["sessions"]
    assert "s1" in sessions and "s2" in sessions
    assert sessions["s1"]["symbols"] == ["AAPL", "NVDA"] or \
           sessions["s1"]["symbols"] == ["NVDA", "AAPL"]
    # Subscriptions block — per-symbol refcount + which sessions.
    subs = snap["subscriptions"]
    nvda = next(s for s in subs if s["service"] == EQ and s["symbol"] == "NVDA")
    assert nvda["refcount"] == 2
    assert set(nvda["sessions"]) == {"s1", "s2"}


# ---- defensive --------------------------------------------------------


def test_remove_nonexistent_subscription_is_noop():
    m = SubscriptionManager()
    assert m.remove("ghost", "tok") == set()


def test_add_empty_symbols_list_is_noop():
    m = SubscriptionManager()
    new = m.add("s1", "t1", EQ, [])
    assert new == set()
    # Session should not be registered from an empty add.
    assert m.snapshot()["session_count"] == 0


def test_subkey_is_hashable_and_comparable():
    a = SubKey(EQ, "NVDA")
    b = SubKey(EQ, "NVDA")
    c = SubKey(EQ, "AAPL")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
