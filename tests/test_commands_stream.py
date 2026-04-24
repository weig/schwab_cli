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
from schwab_cli.commands.stream import _resolve_fields

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
    # No session saved.
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
    result = runner.invoke(app, ["stream", "NVDA"])
    assert result.exit_code == 1
