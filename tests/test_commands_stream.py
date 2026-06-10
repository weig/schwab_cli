"""Tests for the `schwab_cli stream` command.

Mocks the Schwab streamer end-to-end so the test runs offline.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, save as save_config
from schwab_cli.session import Session, save as save_session
from schwab_cli.api.streamer import StreamerInfo
from schwab_cli.commands.stream import _market_time, _resolve_fields

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


# ---- field resolution -------------------------------------------------


def test_resolve_fields_default_includes_all_levelone():
    out = _resolve_fields(None)
    # Default returns the streamer module's default set — pipe-joined numeric IDs.
    assert "0" in out.split(",")


def test_resolve_fields_friendly_names_map_to_numeric():
    out = _resolve_fields("bid,ask,last")
    parts = out.split(",")
    assert "0" in parts  # symbol always included
    assert "1" in parts  # bid
    assert "2" in parts  # ask
    assert "3" in parts  # last


def test_resolve_fields_numeric_passthrough():
    out = _resolve_fields("1,2,33")
    parts = out.split(",")
    assert "1" in parts
    assert "2" in parts
    assert "33" in parts


def test_resolve_fields_dedup_and_preserve_symbol():
    out = _resolve_fields("bid,bid,bid")
    parts = out.split(",")
    assert parts.count("1") == 1
    assert parts.count("0") == 1


# ---- command surface ---------------------------------------------------


def test_stream_mcp_and_direct_mutually_exclusive(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["stream", "NVDA", "--mcp", "--direct"])
    assert result.exit_code == 2


def test_stream_mcp_forced_but_daemon_unreachable_exits_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    # Force MCP path at a port nothing is listening on.
    result = runner.invoke(
        app,
        ["stream", "NVDA", "--mcp", "--mcp-url", "http://127.0.0.1:1"],
    )
    assert result.exit_code == 1


def test_stream_no_session_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    # No session saved. Pin the auto-probe to "no daemon": without the
    # patch it can find a REAL daemon on this machine and stream live
    # quotes forever — test outcomes must never depend on host state.
    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", return_value=False
    ):
        result = runner.invoke(app, ["stream", "NVDA"])
    assert result.exit_code == 1


def test_stream_expired_refresh_token_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=100, refresh_token_expires_at=100,
    ))
    # Same isolation pin as above: exercise the DIRECT path's fail-fast.
    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", return_value=False
    ):
        result = runner.invoke(app, ["stream", "NVDA"])
    assert result.exit_code == 1


def test_market_time_uses_quote_time_in_eastern():
    # 1777037749175 ms = 2026-04-24 13:35:49.175 UTC = 09:35:49.175 EDT
    ts = _market_time({"symbol": "NVDA", "quote_time": 1777037749175})
    assert ts == "09:35:49.175"


def test_market_time_falls_back_to_trade_time():
    ts = _market_time({"symbol": "NVDA", "trade_time": 1777037749091})
    assert ts == "09:35:49.091"


def test_market_time_ignores_non_numeric_timestamp():
    # Falls back to now() — just check format.
    ts = _market_time({"symbol": "NVDA"})
    assert len(ts) == 12 and ts[2] == ":" and ts[5] == ":" and ts[8] == "."


# ---- --direct guard against running daemon ----------------------------


def test_stream_direct_blocked_when_daemon_reachable(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", return_value=True
    ):
        result = runner.invoke(app, ["stream", "NVDA", "--direct"])
    assert result.exit_code == 2, result.stderr
    # Error message should mention both the guidance and the override.
    assert "--force" in result.stderr
    assert "only allows one streamer session" in result.stderr


def test_stream_direct_force_proceeds_when_daemon_reachable(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    called = {"direct": 0}

    async def fake_direct(symbols, *, fields, as_json):
        called["direct"] += 1

    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", return_value=True
    ), patch(
        "schwab_cli.commands.stream._run_direct", side_effect=fake_direct
    ):
        result = runner.invoke(
            app, ["stream", "NVDA", "--direct", "--force"]
        )
    assert result.exit_code == 0, result.output + result.stderr
    assert called["direct"] == 1
    # Warning should surface on stderr so the user can see what they opted into.
    assert "--force" in result.stderr


def test_stream_direct_proceeds_when_daemon_unreachable(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    called = {"direct": 0}

    async def fake_direct(symbols, *, fields, as_json):
        called["direct"] += 1

    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", return_value=False
    ), patch(
        "schwab_cli.commands.stream._run_direct", side_effect=fake_direct
    ):
        result = runner.invoke(app, ["stream", "NVDA", "--direct"])
    assert result.exit_code == 0, result.output + result.stderr
    assert called["direct"] == 1


# ---- auto-MCP mid-stream drop: re-probe before falling back to direct -----


def test_stream_auto_mcp_drop_aborts_when_daemon_still_up(monkeypatch, tmp_path):
    """If the daemon is still up after the MCP stream drops, do NOT open a
    direct streamer (it would evict the daemon) — abort with exit 1."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.commands._stream_mcp import McpUnreachable
    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", side_effect=[True, True]
    ), patch(
        "schwab_cli.commands.stream._run_via_mcp",
        side_effect=McpUnreachable("drop"),
    ), patch(
        "schwab_cli.commands.stream._run_direct"
    ) as direct:
        result = runner.invoke(app, ["stream", "NVDA"])
    assert result.exit_code == 1, result.stderr
    assert "still running" in result.stderr
    assert not direct.called


def test_stream_auto_mcp_drop_falls_back_when_daemon_gone(monkeypatch, tmp_path):
    """If the daemon really went away, fall back to a direct streamer."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.commands._stream_mcp import McpUnreachable
    called = {"direct": 0}

    async def fake_direct(symbols, *, fields, as_json):
        called["direct"] += 1

    with patch(
        "schwab_cli.commands.stream._probe_mcp_daemon", side_effect=[True, False]
    ), patch(
        "schwab_cli.commands.stream._run_via_mcp",
        side_effect=McpUnreachable("drop"),
    ), patch(
        "schwab_cli.commands.stream._run_direct", side_effect=fake_direct
    ):
        result = runner.invoke(app, ["stream", "NVDA"])
    assert result.exit_code == 0, result.stderr
    assert called["direct"] == 1
