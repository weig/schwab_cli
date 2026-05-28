from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.fundamentals import FundamentalsService
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
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14},
        "fundamental": {"peRatio": 33.85, "eps": 6.54},
    },
}


def test_get_fundamentals_wraps_payload_and_symbols(configured_home):
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_PAYLOAD):
        result = FundamentalsService().get_fundamentals(["AAPL"])

    assert result.symbols == ("AAPL",)
    assert result.payload == _PAYLOAD


def test_get_fundamentals_normalizes_class_share_symbols(configured_home):
    payload = {"BRK/B": {"symbol": "BRK/B", "quote": {"lastPrice": 450.0}}}
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=payload):
        result = FundamentalsService().get_fundamentals(["brk.b"])

    # Renderer keys off the normalized canonical form Schwab returns.
    assert result.symbols == ("BRK/B",)


def test_get_fundamentals_preserves_input_order(configured_home):
    with patch("schwab_cli.api.client.SchwabClient.get", return_value={}):
        result = FundamentalsService().get_fundamentals(["MSFT", "AAPL", "NVDA"])

    assert result.symbols == ("MSFT", "AAPL", "NVDA")


def test_get_fundamentals_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        FundamentalsService().get_fundamentals(["AAPL"])


def test_get_fundamentals_no_session_raises_not_authenticated(monkeypatch, tmp_path):
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
        FundamentalsService().get_fundamentals(["AAPL"])
