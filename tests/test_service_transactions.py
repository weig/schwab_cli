"""Unit tests for the Layer-2 ``service.transactions.get_transactions``.

These exercise the service in isolation from the command shim. The stable
``schwab_cli.api.transactions_cache.fetch_cached`` seam is mocked so no real
HTTP or DB happens. A future-dated session keeps ``service.auth.get_session``
from attempting an ``oauth.refresh``.

Coverage:
  - happy path: fetch -> filter -> shape produces a TransactionsResult with
    shaped rows, the show_account signal, and forwarded cache_stats;
  - the ``--type`` filter culls non-matching rows BEFORE shaping;
  - the parsed range start/end and refresh flag are forwarded verbatim to
    fetch_cached, and the account positional is threaded through;
  - ``show_account`` is False when an account is supplied, True otherwise;
  - ``NotConfigured`` / ``NotAuthenticated`` propagate when config/session
    are absent.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.transactions import TransactionsService
from schwab_cli.service.types import TransactionsResult
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

_FETCH_CACHED = "schwab_cli.api.transactions_cache.fetch_cached"

_START = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
_END = datetime(2026, 4, 18, 23, 59, 59, tzinfo=timezone.utc)

# A TRADE and a DIVIDEND_OR_INTEREST so the --type filter has something to cull.
_RAW = [
    {
        "_account": "12340756",
        "activityId": 1,
        "time": "2026-04-15T10:00:00+0000",
        "type": "TRADE",
        "netAmount": -1055.30,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "AMZN"},
                "amount": 5.0, "cost": -1055.30, "price": 211.06,
                "positionEffect": "OPENING",
            },
        ],
    },
    {
        "_account": "12340756",
        "activityId": 2,
        "time": "2026-04-16T10:00:00+0000",
        "type": "DIVIDEND_OR_INTEREST",
        "description": "THE COCA-COLA CO",
        "netAmount": 22.31,
        "transferItems": [
            {
                "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
                "amount": 22.31, "cost": 0.0, "price": 0.0,
            },
        ],
    },
]


def _prep_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


def _call(account=None, *, type_filter="ALL", refresh=False):
    return TransactionsService().get_transactions(
        account,
        start=_START,
        end=_END,
        type_filter=type_filter,
        refresh=refresh,
    )


def test_happy_path_fetch_filter_shape(monkeypatch, tmp_path):
    """fetch -> filter(ALL) -> shape produces sorted shaped rows."""
    _prep_auth(monkeypatch, tmp_path)
    with patch(_FETCH_CACHED, return_value=list(_RAW)) as m_fetch:
        result = _call(type_filter="ALL")
    m_fetch.assert_called_once()
    assert isinstance(result, TransactionsResult)
    # Both rows survive ALL; shaped + sorted ascending by time.
    assert len(result.rows) == 2
    assert result.rows[0]["symbol"] == "AMZN"
    assert result.rows[0]["type"] == "TRADE"
    # Dividend currency leg surfaces the description as symbol.
    assert result.rows[1]["symbol"] == "THE COCA-COLA CO"
    assert result.show_account is True


def test_type_filter_culls_before_shape(monkeypatch, tmp_path):
    """--type TRADE drops the dividend row before shaping."""
    _prep_auth(monkeypatch, tmp_path)
    with patch(_FETCH_CACHED, return_value=list(_RAW)):
        result = _call(type_filter="TRADE")
    assert len(result.rows) == 1
    assert result.rows[0]["type"] == "TRADE"
    assert result.rows[0]["symbol"] == "AMZN"


def test_range_and_refresh_forwarded(monkeypatch, tmp_path):
    """Parsed start/end and refresh flag reach fetch_cached unchanged."""
    _prep_auth(monkeypatch, tmp_path)
    with patch(_FETCH_CACHED, return_value=[]) as m_fetch:
        _call(type_filter="ALL", refresh=True)
    _, kwargs = m_fetch.call_args
    assert kwargs["start"] is _START
    assert kwargs["end"] is _END
    assert kwargs["refresh"] is True


def test_account_threaded_and_show_account_false(monkeypatch, tmp_path):
    """An explicit account is the second positional arg; show_account False."""
    _prep_auth(monkeypatch, tmp_path)
    with patch(_FETCH_CACHED, return_value=[]) as m_fetch:
        result = _call(account="0756")
    args, _ = m_fetch.call_args
    assert args[1] == "0756"
    assert result.show_account is False


def test_cache_stats_forwarded(monkeypatch, tmp_path):
    """fetch_cached's out-parameter stats surface on the result verbatim."""
    _prep_auth(monkeypatch, tmp_path)

    def fake_fetch(client, account_number, *, start, end, refresh, stats):
        stats["total"] = 2
        stats["from_api"] = 1
        stats["from_cache"] = 1
        return list(_RAW)

    with patch(_FETCH_CACHED, side_effect=fake_fetch):
        result = _call(type_filter="ALL")
    assert result.cache_stats == {"total": 2, "from_api": 1, "from_cache": 1}


def test_no_config_raises_not_configured(monkeypatch, tmp_path):
    """No config on disk -> NotConfigured, before any fetch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    with patch(_FETCH_CACHED) as m_fetch:
        with pytest.raises(NotConfigured):
            _call()
    m_fetch.assert_not_called()


def test_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    """Config present but no session file -> NotAuthenticated propagates."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    with patch(_FETCH_CACHED) as m_fetch:
        with pytest.raises(NotAuthenticated):
            _call()
    m_fetch.assert_not_called()
