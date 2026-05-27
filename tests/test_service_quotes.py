from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.quotes import get_quote
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


_PAYLOAD = {
    "errors": {"invalidSymbols": ["NOTREAL"]},
    "AAPL": {
        "symbol": "AAPL",
        "quote": {
            "lastPrice": 232.14,
            "netChange": 1.20,
            "netPercentChangeInDouble": 0.5194,
            "bidPrice": 232.00,
            "askPrice": 232.28,
            "totalVolume": 1_000_000,
        },
    },
}


def test_get_quote_maps_valid_and_invalid_rows_in_order(configured_home):
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_PAYLOAD):
        result = get_quote(["AAPL", "NOTREAL"])

    assert [r.symbol for r in result.rows] == ["AAPL", "NOTREAL"]

    aapl = result.rows[0]
    assert aapl.last == 232.14
    assert aapl.change == 1.20
    assert aapl.change_pct == 0.5194
    assert aapl.bid == 232.00
    assert aapl.ask == 232.28
    assert aapl.volume == 1_000_000
    assert aapl.error is None

    notreal = result.rows[1]
    assert notreal.error == "invalid symbol"
    assert notreal.last is None
    assert notreal.change is None
    assert notreal.change_pct is None
    assert notreal.bid is None
    assert notreal.ask is None
    assert notreal.volume is None


def test_get_quote_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        get_quote(["AAPL"])


def test_get_quote_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    # Config present but no session file -> the auth error propagates
    # through the service boundary unchanged.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    with pytest.raises(NotAuthenticated):
        get_quote(["AAPL"])
