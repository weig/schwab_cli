from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.accounts import AccountsService
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.types import AccountResult, AccountsResult, PositionsResult
from schwab_cli.session import Session
from schwab_cli.session import save as save_session


@pytest.fixture
def configured_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    now = int(time.time())
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=now + 3600,
            refresh_token_expires_at=now + 7 * 24 * 3600,
        )
    )
    return tmp_path


_ACCOUNTS = [
    {
        "securitiesAccount": {
            "accountNumber": "12345678",
            "type": "MARGIN",
            "positions": [],
        }
    },
    {
        "securitiesAccount": {
            "accountNumber": "87654321",
            "type": "CASH",
            "positions": [],
        }
    },
]

_SINGLE_ACCOUNT = {
    "securitiesAccount": {
        "accountNumber": "12345678",
        "type": "MARGIN",
        "positions": [],
    }
}

_POSITION_ROWS = [
    {"_account": "12345678", "instrument": {"symbol": "AAPL"}, "longQuantity": 10.0},
]


# ---------------------------------------------------------------------------
# Happy-path mapping
# ---------------------------------------------------------------------------


def test_list_accounts_wraps_payload_verbatim(configured_home):
    with patch(
        "schwab_cli.api.accounts.list_accounts", return_value=_ACCOUNTS
    ) as mock_list:
        result = AccountsService().list_accounts()
    assert isinstance(result, AccountsResult)
    assert list(result.accounts) == _ACCOUNTS
    # Called via module attribute with the constructed client.
    assert mock_list.call_count == 1


def test_get_account_wraps_payload_verbatim(configured_home):
    with patch(
        "schwab_cli.api.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ) as mock_get:
        result = AccountsService().get_account("12345678")
    assert isinstance(result, AccountResult)
    assert result.account == _SINGLE_ACCOUNT
    # account_number forwarded as the second positional arg.
    assert mock_get.call_args.args[1] == "12345678"


def test_get_positions_wraps_rows_verbatim(configured_home):
    with patch(
        "schwab_cli.api.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = AccountsService().get_positions(None)
    assert isinstance(result, PositionsResult)
    assert list(result.positions) == _POSITION_ROWS


# ---------------------------------------------------------------------------
# Positions filter forwarding
# ---------------------------------------------------------------------------


def test_get_positions_forwards_account_number(configured_home):
    with patch(
        "schwab_cli.api.accounts.get_positions", return_value=_POSITION_ROWS
    ) as mock_pos:
        AccountsService().get_positions("5678")
    assert mock_pos.call_args.args[1] == "5678"


def test_get_positions_forwards_none(configured_home):
    with patch(
        "schwab_cli.api.accounts.get_positions", return_value=_POSITION_ROWS
    ) as mock_pos:
        AccountsService().get_positions(None)
    assert mock_pos.call_args.args[1] is None


# ---------------------------------------------------------------------------
# NotConfigured (no config on disk)
# ---------------------------------------------------------------------------


def test_list_accounts_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        AccountsService().list_accounts()


def test_get_account_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        AccountsService().get_account("12345678")


def test_get_positions_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        AccountsService().get_positions(None)


# ---------------------------------------------------------------------------
# NotAuthenticated (config but no session)
# ---------------------------------------------------------------------------


def _config_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )


def test_list_accounts_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    _config_only(monkeypatch, tmp_path)
    with pytest.raises(NotAuthenticated):
        AccountsService().list_accounts()


def test_get_account_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    _config_only(monkeypatch, tmp_path)
    with pytest.raises(NotAuthenticated):
        AccountsService().get_account("12345678")


def test_get_positions_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    _config_only(monkeypatch, tmp_path)
    with pytest.raises(NotAuthenticated):
        AccountsService().get_positions(None)
