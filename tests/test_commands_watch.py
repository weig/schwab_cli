"""End-to-end CLI tests for `schwab watch add/remove/list`."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.dataset.store import (
    list_watched_symbols,
    read_ticker_state,
    subscribe_equity,
    subscribe_index,
)
from schwab_cli.storage import vol_history


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))


def test_add_subscribes_to_both_groups(runner):
    result = runner.invoke(app, ["watch", "add", "nvda"])
    assert result.exit_code == 0, result.output
    with vol_history.connect() as conn:
        assert list_watched_symbols(conn) == ["NVDA"]
        # One row per group must exist with source='watch'.
        rows = conn.execute(
            "SELECT group_name FROM subscriptions "
            "WHERE symbol=? AND source='watch' AND unsubscribed_at IS NULL",
            ("NVDA",),
        ).fetchall()
    assert sorted(r["group_name"] for r in rows) == ["ohlcv", "volatility"]


def test_remove_demotes_to_grace_when_no_other_source(runner):
    runner.invoke(app, ["watch", "add", "NVDA"])
    result = runner.invoke(app, ["watch", "remove", "NVDA"])
    assert result.exit_code == 0, result.output
    assert "GRACE" in result.output
    with vol_history.connect() as conn:
        assert list_watched_symbols(conn) == []
        for g in ("ohlcv", "volatility"):
            ts = read_ticker_state(conn, symbol="NVDA", group_name=g)
            assert ts is not None, f"missing tier row for {g}"
            assert ts["tier"] == "GRACE"


def test_remove_leaves_other_source_untouched(runner):
    """When another source (e.g. account position) still subscribes the
    symbol, remove should NOT demote — the data is still flowing."""
    runner.invoke(app, ["watch", "add", "NVDA"])
    with vol_history.connect() as conn:
        subscribe_equity(conn, symbol="NVDA", group_name="volatility")
        subscribe_equity(conn, symbol="NVDA", group_name="ohlcv")
    result = runner.invoke(app, ["watch", "remove", "NVDA"])
    assert result.exit_code == 0
    assert "GRACE" not in result.output
    with vol_history.connect() as conn:
        for g in ("ohlcv", "volatility"):
            ts = read_ticker_state(conn, symbol="NVDA", group_name=g)
            # No demotion happened — either no state row or tier
            # whatever the evaluator wrote (we never wrote one here).
            assert ts is None or ts["tier"] != "GRACE"


def test_remove_skips_demotion_when_symbol_in_indices(runner):
    """Per spec: only demote to GRACE if the symbol is NOT in indices."""
    runner.invoke(app, ["watch", "add", "AAPL"])
    with vol_history.connect() as conn:
        # Pretend AAPL is in SPX — write the indices subscription row
        # that `dataset update --indices` would have written.
        conn.execute(
            """
            INSERT INTO subscriptions
              (symbol, group_name, source, source_key,
               subscribed_at, unsubscribed_at)
            VALUES ('AAPL', 'volatility', 'indices', 'SPX', 1000, NULL)
            """
        )
        conn.commit()
    result = runner.invoke(app, ["watch", "remove", "AAPL"])
    assert result.exit_code == 0
    assert "GRACE" not in result.output


def test_list_empty_when_nothing_added(runner):
    result = runner.invoke(app, ["watch", "list"])
    assert result.exit_code == 0
    assert "empty" in result.output


# ---- `watch show` daemon-first routing (Phase 3) ---------------------------
#
# patch() auto-creates AsyncMock for the async _run_show_* functions, so a
# plain patch is an awaitable no-op and side_effect=<exc> raises on await.

from unittest.mock import patch  # noqa: E402

import typer  # noqa: E402

from schwab_cli.commands import watch as watch_cmd  # noqa: E402
from schwab_cli.commands._stream_mcp import McpUnreachable  # noqa: E402

_LWS = "schwab_cli.dataset.store.list_watched_symbols"
_PROBE = "schwab_cli.commands.watch.probe_daemon"
_VIA_MCP = "schwab_cli.commands.watch._run_show_via_mcp"
_DIRECT = "schwab_cli.commands.watch._run_show_direct"


def test_watch_show_routes_through_daemon_when_reachable():
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, return_value=True), \
         patch(_VIA_MCP) as mcp, \
         patch(_DIRECT) as direct:
        watch_cmd.run_show()
    assert mcp.called
    assert not direct.called


def test_watch_show_uses_direct_when_no_daemon():
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, return_value=False), \
         patch(_VIA_MCP) as mcp, \
         patch(_DIRECT) as direct:
        watch_cmd.run_show()
    assert direct.called
    assert not mcp.called


def test_watch_show_direct_refused_when_daemon_running():
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, return_value=True), \
         patch(_VIA_MCP) as mcp, \
         patch(_DIRECT) as direct:
        with pytest.raises(typer.Exit) as ei:
            watch_cmd.run_show(direct=True, force=False)
    assert ei.value.exit_code == 2
    assert not mcp.called
    assert not direct.called


def test_watch_show_direct_force_proceeds_when_daemon_running():
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, return_value=True), \
         patch(_VIA_MCP) as mcp, \
         patch(_DIRECT) as direct:
        watch_cmd.run_show(direct=True, force=True)
    assert direct.called
    assert not mcp.called


def test_watch_show_falls_back_to_direct_when_daemon_gone():
    # Initial probe picks MCP (True); after McpUnreachable the re-probe
    # returns False (daemon really gone) → safe to open direct.
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, side_effect=[True, False]), \
         patch(_VIA_MCP, side_effect=McpUnreachable("boom")) as mcp, \
         patch(_DIRECT) as direct:
        watch_cmd.run_show()
    assert mcp.called
    assert direct.called


def test_watch_show_aborts_instead_of_evicting_live_daemon_on_mcp_failure():
    # Initial probe picks MCP (True); after McpUnreachable the re-probe is
    # still True (daemon up) → must NOT open a direct streamer (would kick
    # the daemon); aborts with exit 1 instead.
    with patch(_LWS, return_value=["NVDA"]), \
         patch(_PROBE, side_effect=[True, True]), \
         patch(_VIA_MCP, side_effect=McpUnreachable("boom")) as mcp, \
         patch(_DIRECT) as direct:
        with pytest.raises(typer.Exit) as ei:
            watch_cmd.run_show()
    assert ei.value.exit_code == 1
    assert mcp.called
    assert not direct.called


def test_watch_show_empty_watchlist_does_not_probe():
    with patch(_LWS, return_value=[]), \
         patch(_PROBE) as probe:
        watch_cmd.run_show()
    assert not probe.called
