"""Tests for `schwab_cli mcp install`."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from schwab_cli.cli import app

runner = CliRunner()


def test_install_creates_new_settings_sse(tmp_path):
    settings = tmp_path / "settings.json"
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings),
         "--yes", "--url", "http://127.0.0.1:7234"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    entry = data["mcpServers"]["schwab"]
    assert entry["type"] == "sse"
    assert entry["url"].endswith("/sse")


def test_install_stdio_emits_spawn_form(tmp_path):
    settings = tmp_path / "settings.json"
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings),
         "--yes", "--stdio"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    entry = data["mcpServers"]["schwab"]
    assert entry["command"] == "schwab_cli"
    assert entry["args"] == ["mcp", "--stdio"]


def test_install_with_token_adds_authorization_header(tmp_path):
    settings = tmp_path / "settings.json"
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings),
         "--yes", "--token", "SECRET"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    entry = data["mcpServers"]["schwab"]
    assert entry["headers"] == {"Authorization": "Bearer SECRET"}


def test_install_refuses_overwrite_without_force(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"schwab": {"foo": "bar"}}}))
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings), "--yes"],
    )
    assert result.exit_code == 1


def test_install_force_overwrites_existing_entry(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"schwab": {"foo": "bar"}}}))
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings),
         "--yes", "--force"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    assert "foo" not in data["mcpServers"]["schwab"]


def test_install_preserves_other_settings_keys(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "theme": "dark",
        "mcpServers": {"other": {"command": "other_thing"}},
    }))
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings), "--yes"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert "other" in data["mcpServers"]
    assert "schwab" in data["mcpServers"]


def test_install_rejects_non_object_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(["not", "an", "object"]))
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings), "--yes"],
    )
    assert result.exit_code == 1


def test_install_rejects_invalid_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ broken json")
    result = runner.invoke(
        app,
        ["mcp", "install", "--claude-settings", str(settings), "--yes"],
    )
    assert result.exit_code == 1
