from __future__ import annotations

import time
from datetime import date
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.chains import ChainsService
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


_RAW_CHAIN = {
    "symbol": "AMZN",
    "underlying": {"last": 255.0, "change": 1.0, "percentChange": 0.4},
    "callExpDateMap": {
        "2026-05-01:4": {
            "255.0": [
                {
                    "symbol": "AMZN_050126C255",
                    "strikePrice": 255.0,
                    "bid": 3.0,
                    "ask": 3.2,
                    "delta": 0.52,
                    "volatility": 32.5,
                    "expirationDate": "2026-05-01T00:00:00.000+00:00",
                    "daysToExpiration": 4,
                }
            ]
        }
    },
    "putExpDateMap": {},
}


def test_get_chain_envelope_returns_shaped_envelope(configured_home):
    expiry = date(2026, 5, 1)
    with patch(
        "schwab_cli.service.chains.api_chains.get_chain",
        return_value=_RAW_CHAIN,
    ) as mock_get_chain:
        envelope = ChainsService().get_chain_envelope("AMZN", expiry=expiry, strike_count=10)

    # Service reaches Layer-1 via the module attribute with the args the
    # MCP tool used to pass inline.
    mock_get_chain.assert_called_once()
    _, kwargs = mock_get_chain.call_args
    assert kwargs["contract_type"] == "ALL"
    assert kwargs["strike_count"] == 10
    assert kwargs["from_date"] == expiry
    assert kwargs["to_date"] == expiry

    # Envelope shape from shape_envelope.
    assert envelope["symbol"] == "AMZN"
    assert envelope["underlying"]["last"] == 255.0
    assert len(envelope["contracts"]) == 1
    contract = envelope["contracts"][0]
    assert contract["side"] == "C"
    assert contract["strike"] == 255.0
    # IV normalized from percent to decimal.
    assert contract["iv"] == pytest.approx(0.325)


def test_get_chain_envelope_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        ChainsService().get_chain_envelope("AMZN", expiry=date(2026, 5, 1))


def test_get_chain_envelope_no_session_raises_not_authenticated(
    monkeypatch, tmp_path
):
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
        ChainsService().get_chain_envelope("AMZN", expiry=date(2026, 5, 1))
