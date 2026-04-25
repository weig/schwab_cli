"""Inbound Telegram polling tests.

All HTTP mocked via respx — never reaches the real Telegram bot API.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from schwab_cli.notify.telegram_poll import (
    TelegramPoller,
    load_claude_allowlist,
    wait_for_text_reply,
)


_BOT = "TESTBOT"
_CHAT = "1234567"
_API = f"https://api.telegram.org/bot{_BOT}"


def _msg(*, text: str, chat_id=_CHAT, user_id=999, update_id=100):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "text": text,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
        },
    }


# ---- drain ----------------------------------------------------------------


@respx.mock
def test_drain_records_baseline_when_pending_updates_exist():
    respx.get(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "result": [_msg(text="old", update_id=42)],
        }),
    )
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)

    async def run():
        await poller.drain()
    asyncio.run(run())

    assert poller._last_update_id == 42


@respx.mock
def test_drain_records_zero_when_no_pending():
    respx.get(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []}),
    )
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)
    asyncio.run(poller.drain())
    assert poller._last_update_id == 0


# ---- wait_for_reply ------------------------------------------------------


@respx.mock
def test_wait_for_reply_returns_matched_message():
    # First call: drain — empty.
    # Second call: returns CONFIRM_OVERRIDE.
    routes = respx.get(f"{_API}/getUpdates")
    routes.side_effect = [
        httpx.Response(200, json={"ok": True, "result": []}),                # drain
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="CONFIRM_OVERRIDE", update_id=101)],
        }),
    ]
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)

    async def run():
        await poller.drain()
        return await poller.wait_for_reply(
            predicate=lambda m: m.get("text") == "CONFIRM_OVERRIDE",
            timeout_seconds=2,
        )

    msg = asyncio.run(run())
    assert msg is not None
    assert msg["text"] == "CONFIRM_OVERRIDE"


@respx.mock
def test_wait_for_reply_returns_none_on_timeout():
    respx.get(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []}),
    )
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)

    async def run():
        await poller.drain()
        return await poller.wait_for_reply(
            predicate=lambda m: m.get("text") == "anything",
            timeout_seconds=1,
        )

    out = asyncio.run(run())
    assert out is None


@respx.mock
def test_wait_for_reply_skips_messages_from_wrong_chat():
    routes = respx.get(f"{_API}/getUpdates")
    routes.side_effect = [
        # Drain.
        httpx.Response(200, json={"ok": True, "result": []}),
        # First poll: stranger chat. Wrong chat → skip.
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="hello", chat_id="9999999", update_id=200)],
        }),
        # Second poll: correct chat with the keyword.
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="GO", update_id=201)],
        }),
    ]
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)

    async def run():
        await poller.drain()
        return await poller.wait_for_reply(
            predicate=lambda m: m.get("text") == "GO",
            timeout_seconds=2,
        )

    msg = asyncio.run(run())
    assert msg is not None
    assert msg["text"] == "GO"


@respx.mock
def test_wait_for_reply_enforces_user_allowlist_when_set():
    routes = respx.get(f"{_API}/getUpdates")
    routes.side_effect = [
        # Drain.
        httpx.Response(200, json={"ok": True, "result": []}),
        # Right chat, wrong user.
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="GO", user_id=666, update_id=300)],
        }),
        # Right chat, allowed user.
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="GO", user_id=999, update_id=301)],
        }),
    ]
    poller = TelegramPoller(
        bot_token=_BOT, chat_id=_CHAT,
        allowed_user_ids=frozenset({999}),
    )

    async def run():
        await poller.drain()
        return await poller.wait_for_reply(
            predicate=lambda m: m.get("text") == "GO",
            timeout_seconds=2,
        )

    msg = asyncio.run(run())
    # The first GO came from a non-allowlisted user and was filtered;
    # the second GO from id=999 wins.
    assert msg is not None
    assert (msg.get("from") or {}).get("id") == 999


@respx.mock
def test_wait_for_reply_retries_on_network_error():
    """Transient httpx.RequestError shouldn't abort the wait."""
    request_count = {"n": 0}

    def _route(_request):
        request_count["n"] += 1
        if request_count["n"] == 1:
            return httpx.Response(200, json={"ok": True, "result": []})
        if request_count["n"] == 2:
            # Simulate a network glitch.
            raise httpx.ConnectError("transient")
        return httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="DONE", update_id=400)],
        })

    respx.get(f"{_API}/getUpdates").mock(side_effect=_route)
    poller = TelegramPoller(bot_token=_BOT, chat_id=_CHAT)

    async def run():
        await poller.drain()
        return await poller.wait_for_reply(
            predicate=lambda m: m.get("text") == "DONE",
            timeout_seconds=4,
        )

    msg = asyncio.run(run())
    assert msg is not None
    assert msg["text"] == "DONE"


# ---- sync wrapper --------------------------------------------------------


@respx.mock
def test_wait_for_text_reply_sync_happy_path():
    routes = respx.get(f"{_API}/getUpdates")
    routes.side_effect = [
        httpx.Response(200, json={"ok": True, "result": []}),
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="CONFIRM_OVERRIDE", update_id=500)],
        }),
    ]
    out = wait_for_text_reply(
        bot_token=_BOT, chat_id=_CHAT,
        expected_text="CONFIRM_OVERRIDE",
        timeout_seconds=2,
    )
    assert out == "CONFIRM_OVERRIDE"


@respx.mock
def test_wait_for_text_reply_case_insensitive():
    routes = respx.get(f"{_API}/getUpdates")
    routes.side_effect = [
        httpx.Response(200, json={"ok": True, "result": []}),
        httpx.Response(200, json={
            "ok": True,
            "result": [_msg(text="confirm_override", update_id=501)],
        }),
    ]
    out = wait_for_text_reply(
        bot_token=_BOT, chat_id=_CHAT,
        expected_text="CONFIRM_OVERRIDE",
        case_sensitive=False, timeout_seconds=2,
    )
    assert out == "confirm_override"


@respx.mock
def test_wait_for_text_reply_timeout_returns_none():
    respx.get(f"{_API}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []}),
    )
    out = wait_for_text_reply(
        bot_token=_BOT, chat_id=_CHAT,
        expected_text="NEVER_ARRIVES",
        timeout_seconds=1,
    )
    assert out is None


# ---- claude allowlist loader ---------------------------------------------


def test_load_claude_allowlist_returns_int_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".claude" / "channels" / "telegram" / "access.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "dmPolicy": "pairing",
        "allowFrom": ["1234", 5678, "not_a_number"],
        "groups": {}, "pending": {},
    }))
    out = load_claude_allowlist()
    assert out == frozenset({1234, 5678})


def test_load_claude_allowlist_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = load_claude_allowlist()
    assert out == frozenset()


def test_load_claude_allowlist_malformed_json_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".claude" / "channels" / "telegram" / "access.json"
    p.parent.mkdir(parents=True)
    p.write_text("not valid json {{{")
    out = load_claude_allowlist()
    assert out == frozenset()
