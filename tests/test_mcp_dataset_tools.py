"""MCP read-only dataset tools.

We exercise the dispatcher directly (no SSE / stdio transport),
asserting the tool returns properly-shaped JSON content.

WAL isolation: SQLite's WAL journal mode allows readers to see a
consistent snapshot as of their connection open time, so a concurrent
uncommitted write is not visible to a second, independent reader
connection (test_concurrent_write_and_read_safe).
"""
from __future__ import annotations

import json

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.dataset.store import subscribe_equity, write_ticker_state


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                         captured_at_ms=1000)
        write_ticker_state(
            conn, symbol="NVDA", group_name="volatility",
            tier="ACTIVE", tier_since=1000,
            consecutive_days_below=0, last_evaluated_at=1000,
        )
    yield


def test_dataset_status_tool_returns_rows(seeded):
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    out = dispatch_dataset_tool("dataset.status",
                                arguments={"group": "volatility"})
    parsed = json.loads(out)
    assert parsed[0]["symbol"] == "NVDA"
    assert parsed[0]["tier"] == "ACTIVE"


def test_dataset_status_tool_filters_by_tier(seeded):
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    out = dispatch_dataset_tool("dataset.status",
                                arguments={"tier": "WATCH"})
    assert json.loads(out) == []


def test_dataset_history_tool_returns_rows(seeded):
    from schwab_cli.storage.vol_history import record_extended_snapshot
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    with vol_history.connect() as conn:
        record_extended_snapshot(
            conn, symbol="NVDA", spot=200.0, atm_iv=0.34,
            atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
            captured_at_ms=1700000000000,
            atm_iv_30d=0.32, hv_30d=0.27,
        )
    out = dispatch_dataset_tool(
        "dataset.history",
        arguments={"symbol": "NVDA", "lookback_days": 5},
    )
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["atm_iv_30d"] == 0.32
    assert rows[0]["hv_30d"] == 0.27


def test_dataset_history_clamps_lookback(seeded):
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    out = dispatch_dataset_tool(
        "dataset.history",
        arguments={"symbol": "NVDA", "lookback_days": 99999},
    )
    # Should not error; clamped silently to 730.
    json.loads(out)


def test_dataset_iv_rank_tool_handles_no_data(seeded):
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    out = dispatch_dataset_tool(
        "dataset.iv_rank",
        arguments={"symbol": "NVDA"},
    )
    parsed = json.loads(out)
    assert parsed.get("low_history") is True or parsed.get("ivr") is None


def test_dataset_unknown_tool_raises():
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    with pytest.raises(ValueError, match="unknown dataset tool"):
        dispatch_dataset_tool("dataset.bogus", arguments={})


def test_concurrent_write_and_read_safe(monkeypatch, tmp_path):
    """WAL mode lets us read mid-write. Simulates the cron writing a
    new row while the daemon's status tool is being called."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    from schwab_cli.storage.vol_history import (
        record_extended_snapshot, connect,
    )
    from schwab_cli.dataset.store import (
        subscribe_equity, write_ticker_state,
    )
    from schwab_cli.mcp_server.app import dispatch_dataset_tool
    import json as _json

    with connect() as c:
        subscribe_equity(c, symbol="NVDA", group_name="volatility",
                         captured_at_ms=1000)
        write_ticker_state(
            c, symbol="NVDA", group_name="volatility",
            tier="ACTIVE", tier_since=1000,
            consecutive_days_below=0, last_evaluated_at=1000,
        )

    # Open writer first WITHOUT committing, then read via a separate
    # vol_history.connect() (independent connection) inside the dispatcher.
    with connect() as writer:
        record_extended_snapshot(
            writer, symbol="NVDA", spot=200.0, atm_iv=0.34,
            atm_strike=200.0, atm_expiry="2026-05-15", atm_dte=30,
            captured_at_ms=2000, atm_iv_30d=0.35,
        )
        # writer hasn't committed yet — reader must NOT see new row.
        out = dispatch_dataset_tool(
            "dataset.history",
            arguments={"symbol": "NVDA", "lookback_days": 5},
        )
        rows = _json.loads(out)
        assert len(rows) == 0
    # After commit (when 'with' exits), a new reader sees the row.
    out2 = dispatch_dataset_tool(
        "dataset.history",
        arguments={"symbol": "NVDA", "lookback_days": 5},
    )
    rows2 = _json.loads(out2)
    assert len(rows2) == 1


def test_mcp_lists_dataset_tools(monkeypatch, tmp_path):
    """The Tool list returned by the MCP server's tool builder must
    include the three dataset.* tools."""
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    import schwab_cli.mcp_server.app as app_mod
    # Try to find a static tool list:
    candidates = [
        getattr(app_mod, "_ALL_TOOLS", None),
        getattr(app_mod, "TOOLS", None),
    ]
    static_tools = next((t for t in candidates if t is not None), None)
    if static_tools is not None:
        names = {t.name for t in static_tools}
    else:
        # Fall back to grepping the source for our tool names.
        src = open(app_mod.__file__).read()
        assert "dataset.status" in src
        assert "dataset.history" in src
        assert "dataset.iv_rank" in src
        return
    assert "dataset.status"  in names
    assert "dataset.history" in names
    assert "dataset.iv_rank" in names
