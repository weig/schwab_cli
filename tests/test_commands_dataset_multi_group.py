"""`schwab_cli dataset subscribe NVDA --group=ohlcv,volatility` adds
one subscription row per product."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.storage import vol_history
from schwab_cli.storage.groups import GROUP_OHLCV, GROUP_VOLATILITY


runner = CliRunner()


def _active_groups_for(conn, symbol: str) -> set[str]:
    return {
        r["group_name"] for r in conn.execute(
            "SELECT group_name FROM subscriptions "
            "WHERE symbol = ? AND unsubscribed_at IS NULL",
            (symbol,),
        ).fetchall()
    }


def test_multi_group_subscribe_creates_one_row_per_product(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)

    result = runner.invoke(app, [
        "dataset", "subscribe", "NVDA",
        "--group", "ohlcv,volatility",
    ])
    assert result.exit_code == 0, result.output

    with vol_history.connect() as conn:
        assert _active_groups_for(conn, "NVDA") == {GROUP_OHLCV, GROUP_VOLATILITY}


def test_single_group_keeps_old_behaviour(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)

    result = runner.invoke(app, [
        "dataset", "subscribe", "AAPL", "--group", "volatility",
    ])
    assert result.exit_code == 0, result.output

    with vol_history.connect() as conn:
        assert _active_groups_for(conn, "AAPL") == {GROUP_VOLATILITY}


def test_unknown_group_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)

    result = runner.invoke(app, [
        "dataset", "subscribe", "AAPL",
        "--group", "volatility,bogus",
    ])
    assert result.exit_code == 2
    assert "unknown group" in result.output.lower()


def test_empty_group_list_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)

    result = runner.invoke(app, [
        "dataset", "subscribe", "AAPL", "--group", " , ",
    ])
    assert result.exit_code == 2
    assert "at least one product" in result.output.lower()
