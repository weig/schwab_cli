"""Tests for the notify package — config, telegram formatting,
dispatcher, rate limiting, Slack stub."""

from __future__ import annotations

import io
import json

import httpx
import pytest
import respx

from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.notify import Notifier
from schwab_cli.notify import config as notify_config
from schwab_cli.notify import slack as slack_channel
from schwab_cli.notify import telegram as tg


# ---- config load / save -----------------------------------------------


def test_load_missing_file_returns_empty_config(tmp_path):
    cfg = notify_config.load(tmp_path / "nope.json")
    assert cfg.telegram.bot_token is None
    assert cfg.telegram.configured is False
    assert cfg.any_configured is False


def test_load_malformed_json_is_tolerated(tmp_path):
    p = tmp_path / "n.json"
    p.write_text("{ broken json")
    cfg = notify_config.load(p)
    assert cfg.telegram.bot_token is None


def test_load_missing_rate_limit_uses_default(tmp_path):
    p = tmp_path / "n.json"
    p.write_text(json.dumps({"telegram": {"bot_token": "t", "chat_id": "c",
                                           "events": ["x"]}}))
    cfg = notify_config.load(p)
    assert cfg.telegram.rate_limit_seconds == 300
    assert cfg.telegram.events == ["x"]


def test_load_full_config(tmp_path):
    p = tmp_path / "n.json"
    p.write_text(json.dumps({
        "telegram": {
            "bot_token": "BOT", "chat_id": "CHAT",
            "events": ["auth.auto_login.failed", "streamer.crash"],
            "rate_limit_seconds": 60,
        },
    }))
    cfg = notify_config.load(p)
    assert cfg.telegram.configured is True
    assert cfg.telegram.bot_token == "BOT"
    assert cfg.telegram.chat_id == "CHAT"
    assert cfg.telegram.rate_limit_seconds == 60


def test_save_writes_atomically_with_0600(tmp_path):
    p = tmp_path / "sub" / "n.json"
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="B", chat_id="C", events=["e1"], rate_limit_seconds=60,
        ),
    )
    notify_config.save(cfg, p)
    assert p.exists()
    # Mode check: owner-only read/write.
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600
    data = json.loads(p.read_text())
    assert data["telegram"]["bot_token"] == "B"
    assert "slack" in data
    assert "_tbd" in data["slack"]


def test_save_and_reload_roundtrip(tmp_path):
    p = tmp_path / "n.json"
    before = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="B", chat_id="C", events=["x"],
        ),
    )
    notify_config.save(before, p)
    after = notify_config.load(p)
    assert after.telegram.bot_token == "B"
    assert after.telegram.events == ["x"]


# ---- Telegram formatting ---------------------------------------------


def test_escape_markdown_v2_handles_reserved_chars():
    assert tg.escape_markdown_v2("a.b_c*d") == "a\\.b\\_c\\*d"


def test_format_message_escapes_dots_in_event_name():
    out = tg.format_message(
        "auth.auto_login.failed", "error",
        "Schwab auto-login FAILED.",
        {"stderr_tail": "401: bad token"},
    )
    # Event name's dots and underscores should be escaped.
    assert "auth\\.auto\\_login\\.failed" in out
    # Reserved punctuation in field values also escaped.
    assert "stderr\\_tail" in out
    assert "401" in out


def test_format_message_no_fields_omits_tail():
    out = tg.format_message("x", "info", "summary", {})
    assert "x" in out
    assert "summary" in out


# ---- telegram.send (HTTP mocked) --------------------------------------


@respx.mock
def test_telegram_send_happy_path():
    route = respx.post(
        "https://api.telegram.org/botTOK/sendMessage"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))
    ok, detail = tg.send(bot_token="TOK", chat_id="CID", text="hello")
    assert ok is True
    assert detail == "ok"
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == "CID"
    assert body["parse_mode"] == "MarkdownV2"


@respx.mock
def test_telegram_send_reports_4xx():
    respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        return_value=httpx.Response(400, text="Bad Request: chat not found"),
    )
    ok, detail = tg.send(bot_token="TOK", chat_id="CID", text="hello")
    assert ok is False
    assert "400" in detail
    assert "chat" in detail.lower()


@respx.mock
def test_telegram_send_network_error_swallowed():
    respx.post("https://api.telegram.org/botTOK/sendMessage").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    ok, detail = tg.send(bot_token="TOK", chat_id="CID", text="hello")
    assert ok is False
    assert "network" in detail


# ---- Notifier dispatch + rate limiting -------------------------------


@respx.mock
def test_notifier_emits_through_telegram_when_subscribed():
    route = respx.post(
        "https://api.telegram.org/botBOT/sendMessage"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="BOT", chat_id="CHAT",
            events=["auth.auto_login.failed"],
        ),
    )
    buf = io.StringIO()
    n = Notifier(cfg, logbook=LogBook(stream=buf))
    n.emit("auth.auto_login.failed", stderr_tail="bad token")
    assert route.called


@respx.mock
def test_notifier_skips_events_not_in_subscription_list():
    route = respx.post("https://api.telegram.org/botBOT/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="BOT", chat_id="CHAT",
            events=["auth.auto_login.failed"],  # not streamer.crash
        ),
    )
    n = Notifier(cfg)
    n.emit("streamer.crash", reason="unknown")
    assert not route.called


@respx.mock
def test_notifier_rate_limits_duplicate_event_within_window():
    route = respx.post("https://api.telegram.org/botBOT/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="BOT", chat_id="CHAT",
            events=["auth.auto_login.failed"],
            rate_limit_seconds=60,
        ),
    )
    current = [1000.0]

    def clock():
        return current[0]

    buf = io.StringIO()
    n = Notifier(cfg, logbook=LogBook(stream=buf), clock=clock)
    n.emit("auth.auto_login.failed")
    # Within the rate-limit window — must not send again.
    current[0] = 1030.0
    n.emit("auth.auto_login.failed")
    assert route.call_count == 1
    # Advance past the window — must send again.
    current[0] = 1100.0
    n.emit("auth.auto_login.failed")
    assert route.call_count == 2


def test_notifier_no_channels_configured_is_noop():
    n = Notifier(notify_config.NotificationConfig())
    # Must not raise even with no channels set.
    n.emit("auth.auto_login.failed")


@respx.mock
def test_notifier_transport_error_does_not_raise():
    respx.post("https://api.telegram.org/botBOT/sendMessage").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="BOT", chat_id="CHAT",
            events=["auth.auto_login.failed"],
        ),
    )
    buf = io.StringIO()
    n = Notifier(cfg, logbook=LogBook(stream=buf))
    # Must not raise.
    n.emit("auth.auto_login.failed")
    # Log should record the send failure.
    assert "notify.send_failed" in buf.getvalue()


# ---- channels_summary + from_file -------------------------------------


def test_channels_summary_redacts_secrets():
    cfg = notify_config.NotificationConfig(
        telegram=notify_config.TelegramSettings(
            bot_token="BOT_SECRET", chat_id="CHAT",
            events=["x"], rate_limit_seconds=90,
        ),
    )
    n = Notifier(cfg)
    summary = n.channels_summary()
    assert summary["telegram"]["configured"] is True
    assert summary["telegram"]["events"] == ["x"]
    assert summary["telegram"]["rate_limit_seconds"] == 90
    # Tokens must not appear.
    assert "BOT_SECRET" not in json.dumps(summary)


def test_from_file_loads_default_path(tmp_path, monkeypatch):
    p = tmp_path / "n.json"
    p.write_text(json.dumps({
        "telegram": {"bot_token": "B", "chat_id": "C", "events": ["x"]},
    }))
    n = Notifier.from_file(p)
    assert n.config.telegram.configured is True


# ---- Slack stub --------------------------------------------------------


def test_slack_send_raises_not_yet_supported():
    with pytest.raises(slack_channel.SlackNotYetSupported):
        slack_channel.send(webhook_url="https://x", text="y")
