"""Tests for the MCP server's structured logbook."""

from __future__ import annotations

import io
import json

from schwab_cli.mcp_server.logbook import LogBook


def _stub_clock():
    return "2026-04-24T12:00:00.000Z"


def test_emit_writes_one_json_line_to_stream():
    buf = io.StringIO()
    lb = LogBook(stream=buf, clock=_stub_clock)
    lb.emit("subscribe", session="s1", symbols=["NVDA", "AAPL"])
    line = buf.getvalue().strip()
    entry = json.loads(line)
    assert entry["event"] == "subscribe"
    assert entry["level"] == "info"
    assert entry["session"] == "s1"
    assert entry["symbols"] == ["NVDA", "AAPL"]
    assert entry["ts"] == "2026-04-24T12:00:00.000Z"


def test_info_warning_error_set_level():
    buf = io.StringIO()
    lb = LogBook(stream=buf, clock=_stub_clock)
    lb.info("a")
    lb.warning("b")
    lb.error("c")
    lines = [json.loads(l) for l in buf.getvalue().splitlines()]
    assert [e["level"] for e in lines] == ["info", "warning", "error"]


def test_log_file_is_appended(tmp_path):
    logfile = tmp_path / "mcp.log"
    buf = io.StringIO()
    lb = LogBook(stream=buf, log_file=logfile, clock=_stub_clock)
    lb.emit("e1", symbol="NVDA")
    lb.emit("e2", symbol="AAPL")

    lines = logfile.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "e1"
    assert json.loads(lines[1])["symbol"] == "AAPL"


def test_log_file_parent_dir_created(tmp_path):
    logfile = tmp_path / "nested" / "dir" / "mcp.log"
    buf = io.StringIO()
    lb = LogBook(stream=buf, log_file=logfile, clock=_stub_clock)
    lb.emit("hello")
    assert logfile.exists()


def test_extra_kwargs_become_top_level_keys():
    buf = io.StringIO()
    lb = LogBook(stream=buf, clock=_stub_clock)
    lb.emit("x", custom_field={"a": 1}, another=42)
    entry = json.loads(buf.getvalue().strip())
    assert entry["custom_field"] == {"a": 1}
    assert entry["another"] == 42


def test_write_error_does_not_propagate():
    class BrokenStream:
        def write(self, _):
            raise OSError("disk full")

        def flush(self):
            pass

    lb = LogBook(stream=BrokenStream(), clock=_stub_clock)
    # Must not raise even though the stream fails.
    lb.emit("e1")
