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
