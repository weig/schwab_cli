"""Tests for the screener_* read-only MCP tools."""
from __future__ import annotations

import json
import re

import pytest

from schwab_cli.mcp_server.app import dispatch_screener_tool
from schwab_cli.storage import screener as store
from schwab_cli.storage.vol_history import connect

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))


def _seed():
    with connect() as conn:
        store.write_ranking(conn, ranking_date="2026-07-06", rows=[
            {"rank": 1, "symbol": "FAT", "executable_vrp": 0.05, "put_bid": 6.0},
            {"rank": 2, "symbol": "THIN", "executable_vrp": 0.02, "put_bid": 3.0},
        ])
        store.open_position(conn, open_date="2026-07-06", symbol="FAT",
                            cohort="top", strike=500.0, dte=31, premium_bid=6.0,
                            expiry="2026-08-07")
        conn.commit()


def test_tool_names_match_pattern():
    # Guard against the dotted-name bug that broke Claude Desktop.
    assert _NAME_RE.match("screener_ranking")
    assert _NAME_RE.match("screener_status")


def test_screener_ranking_returns_json():
    _seed()
    out = json.loads(dispatch_screener_tool("screener_ranking", arguments={}))
    assert out["ranking_date"] == "2026-07-06"
    assert [r["symbol"] for r in out["rows"]] == ["FAT", "THIN"]


def test_screener_ranking_limit():
    _seed()
    out = json.loads(
        dispatch_screener_tool("screener_ranking", arguments={"limit": 1})
    )
    assert [r["symbol"] for r in out["rows"]] == ["FAT"]


def test_screener_status_returns_json():
    _seed()
    out = json.loads(dispatch_screener_tool("screener_status", arguments={}))
    assert out["latest_ranking_date"] == "2026-07-06"
    assert out["ledger"]["open"] == 1
    assert out["candidate_pool"][0]["symbol"] == "FAT"


def test_unknown_screener_tool():
    out = json.loads(dispatch_screener_tool("screener_bogus", arguments={}))
    assert "error" in out
