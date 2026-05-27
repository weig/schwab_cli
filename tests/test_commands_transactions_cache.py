"""End-to-end tests for the cached transactions command."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, save as save_config
from schwab_cli.session import Session, save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=10_000_000_000, refresh_token_expires_at=10_000_000_000,
    ))


def test_command_routes_via_cache_not_direct_api(monkeypatch, tmp_path):
    """The command must call ``fetch_cached`` rather than the raw
    ``get_all_transactions``."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached, patch(
        "schwab_cli.api.transactions.get_all_transactions",
    ) as raw:
        runner.invoke(app, ["transactions", "-r", "-7d..now"])
    assert cached.called
    assert not raw.called


def test_type_filter_applied_locally_not_via_api(monkeypatch, tmp_path):
    """The cache always fetches with no type filter; the command then
    filters the returned list. Verify the type kwarg never reaches the
    cache layer."""
    _prep(monkeypatch, tmp_path)
    sample = [
        {"activityId": 1, "type": "TRADE",
         "time": "2026-04-15T10:00:00+00:00",
         "_account": "57410756",
         "transferItems": [{"instrument": {"symbol": "JPM"},
                            "amount": 1, "price": 100,
                            "positionEffect": "OPENING"}]},
        {"activityId": 2, "type": "DIVIDEND_OR_INTEREST",
         "time": "2026-04-16T10:00:00+00:00",
         "_account": "57410756",
         "transferItems": [{"instrument": {"assetType": "CURRENCY",
                                           "symbol": "CURRENCY_USD"},
                            "amount": 0.5}]},
    ]
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached",
        return_value=sample,
    ) as cached:
        result = runner.invoke(
            app, ["transactions", "-r", "-30d..now", "--type", "TRADE"],
        )
    _, kwargs = cached.call_args
    assert "types" not in kwargs and "type_filter" not in kwargs
    assert "JPM" in result.output
    assert "DIVIDEND" not in result.output


def test_refresh_flag_passed_through(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached:
        runner.invoke(app, ["transactions", "-r", "-7d..now", "--refresh"])
    _, kwargs = cached.call_args
    assert kwargs.get("refresh") is True


def test_no_refresh_flag_default_false(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached:
        runner.invoke(app, ["transactions", "-r", "-7d..now"])
    _, kwargs = cached.call_args
    assert kwargs.get("refresh", False) is False


def test_account_flag_passes_account_to_cache(monkeypatch, tmp_path):
    """``--account 0756`` filters the cache fetch to that one account."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached:
        runner.invoke(
            app, ["transactions", "--account", "0756", "-r", "-7d..now"],
        )
    args, _ = cached.call_args
    # fetch_cached(client, account_number, start=..., end=..., refresh=...)
    assert args[1] == "0756"


def test_account_short_flag_works(monkeypatch, tmp_path):
    """``-a 0756`` is the short form of ``--account``."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached:
        runner.invoke(app, ["transactions", "-a", "0756", "-r", "-7d..now"])
    args, _ = cached.call_args
    assert args[1] == "0756"


def test_no_account_passes_none_for_all_accounts(monkeypatch, tmp_path):
    """Omitting ``--account`` queries all accounts (account_number=None)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached", return_value=[],
    ) as cached:
        runner.invoke(app, ["transactions", "-r", "-7d..now"])
    args, _ = cached.call_args
    assert args[1] is None


def test_account_flag_hides_account_column_in_human_output(
    monkeypatch, tmp_path,
):
    _prep(monkeypatch, tmp_path)
    sample = [{
        "activityId": 1, "type": "TRADE",
        "time": "2026-04-15T10:00:00+00:00",
        "_account": "57410756",
        "netAmount": -100.0,
        "transferItems": [{"instrument": {"symbol": "JPM"},
                           "amount": 1, "price": 100,
                           "positionEffect": "OPENING"}],
    }]
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached",
        return_value=sample,
    ):
        result = runner.invoke(
            app, ["transactions", "--account", "0756", "-r", "-7d..now"],
        )
    assert "Account" not in result.output
    assert "0756" not in result.output
    assert "JPM" in result.output  # main payload still rendered


def test_no_account_keeps_account_column_in_human_output(
    monkeypatch, tmp_path,
):
    _prep(monkeypatch, tmp_path)
    sample = [{
        "activityId": 1, "type": "TRADE",
        "time": "2026-04-15T10:00:00+00:00",
        "_account": "57410756",
        "netAmount": -100.0,
        "transferItems": [{"instrument": {"symbol": "JPM"},
                           "amount": 1, "price": 100,
                           "positionEffect": "OPENING"}],
    }]
    with patch(
        "schwab_cli.api.transactions_cache.fetch_cached",
        return_value=sample,
    ):
        result = runner.invoke(app, ["transactions", "-r", "-7d..now"])
    assert "Account" in result.output
    assert "0756" in result.output
