"""Tests for `schwab_cli mcp log`."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from schwab_cli.cli import app

runner = CliRunner()


def _write_log(path, entries):
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_mcp_log_missing_file_exits_zero_with_message(tmp_path):
    logfile = tmp_path / "mcp.log"
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile)],
    )
    assert result.exit_code == 0
    assert "not found" in result.output or "not found" in result.stderr


def test_mcp_log_pretty_prints_entries(tmp_path):
    logfile = tmp_path / "mcp.log"
    _write_log(logfile, [
        {"ts": "2026-04-24T12:00:00.000Z", "level": "info",
         "event": "subscribe", "session": "s1", "symbols": ["NVDA"]},
    ])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile)],
    )
    assert result.exit_code == 0
    assert "subscribe" in result.output
    assert "s1" in result.output
    assert "NVDA" in result.output


def test_mcp_log_json_passes_through_raw(tmp_path):
    logfile = tmp_path / "mcp.log"
    raw = {"ts": "2026-04-24T12:00:00.000Z", "level": "info", "event": "x"}
    _write_log(logfile, [raw])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile), "--json"],
    )
    assert result.exit_code == 0
    # Every non-empty line should be valid JSON.
    for line in result.output.strip().splitlines():
        parsed = json.loads(line)
        assert parsed["event"] == "x"


def test_mcp_log_session_filter(tmp_path):
    logfile = tmp_path / "mcp.log"
    _write_log(logfile, [
        {"ts": "t", "level": "info", "event": "a", "session": "s1"},
        {"ts": "t", "level": "info", "event": "b", "session": "s2"},
        {"ts": "t", "level": "info", "event": "c", "session": "s1"},
    ])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile), "--session", "s1"],
    )
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    # Three entries, filter matches events a and c.
    assert any("a " in l or " a " in l for l in lines)
    assert any("c " in l or " c " in l for l in lines)
    assert not any("b " in l or " b " in l for l in lines)


def test_mcp_log_symbol_filter_matches_list_or_scalar(tmp_path):
    logfile = tmp_path / "mcp.log"
    _write_log(logfile, [
        {"ts": "t", "level": "info", "event": "a", "symbols": ["NVDA", "AAPL"]},
        {"ts": "t", "level": "info", "event": "b", "symbols": ["TSLA"]},
        {"ts": "t", "level": "info", "event": "c", "symbol": "NVDA"},
    ])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile), "--symbol", "NVDA"],
    )
    assert result.exit_code == 0
    lines = [l for l in result.output.strip().splitlines() if l]
    # Should include events "a" and "c", exclude "b".
    assert len(lines) == 2


def test_mcp_log_level_filter_threshold(tmp_path):
    logfile = tmp_path / "mcp.log"
    _write_log(logfile, [
        {"ts": "t", "level": "info", "event": "a"},
        {"ts": "t", "level": "warning", "event": "b"},
        {"ts": "t", "level": "error", "event": "c"},
    ])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile), "--level", "warning"],
    )
    assert result.exit_code == 0
    assert "b" in result.output
    assert "c" in result.output
    # info entry "a" should be filtered out.
    lines = [l for l in result.output.strip().splitlines() if l]
    assert len(lines) == 2


def test_mcp_log_tail_limits_historical_output(tmp_path):
    logfile = tmp_path / "mcp.log"
    _write_log(logfile, [
        {"ts": "t", "level": "info", "event": f"e{i}"} for i in range(10)
    ])
    result = runner.invoke(
        app, ["mcp", "log", "--log-file", str(logfile), "--tail", "3"],
    )
    assert result.exit_code == 0
    lines = [l for l in result.output.strip().splitlines() if l]
    assert len(lines) == 3
    assert "e9" in result.output
    assert "e0" not in result.output
