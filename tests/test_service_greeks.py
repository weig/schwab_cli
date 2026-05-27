"""Unit tests for the Layer-2 ``service.greeks.get_greeks`` function.

These exercise the service in isolation from the command shim. The stable
``SchwabClient.get`` seam is mocked so no real HTTP happens, and a
future-dated session keeps ``service.auth.get_session`` from attempting an
``oauth.refresh``.

Coverage:
  - the correct contract is picked by strike + side (with the nearby strike
    ignored), including the float-drift epsilon;
  - ``ContractNotFound`` is raised (with the right fields) when nothing
    matches;
  - ``NotConfigured`` is raised when no config exists on disk;
  - the returned envelope has the exact shape the renderer consumes.
"""

from __future__ import annotations

import time
from datetime import date
from unittest.mock import patch

import pytest

from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.greeks import ContractNotFound, get_greeks
from schwab_cli.service.types import GreeksResult
from schwab_cli.session import Session
from schwab_cli.session import save as save_session


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
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


_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 202.50, "change": 2.62, "percentChange": 1.31},
    "callExpDateMap": {
        "2026-05-01:9": {
            "202.5": [{
                "putCall": "CALL", "symbol": "NVDA  260501C00202500",
                "bid": 4.70, "ask": 4.80, "last": 4.75, "mark": 4.75,
                "delta": 0.510, "gamma": 0.035, "theta": -0.267, "vega": 0.125,
                "rho": 0.023, "volatility": 36.582, "strikePrice": 202.5,
                "totalVolume": 8809, "openInterest": 5174,
                "timeValue": 4.75, "intrinsicValue": 0.0, "inTheMoney": False,
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2026-05-01", "daysToExpiration": 9,
            }],
            "200.0": [{
                "putCall": "CALL", "symbol": "NVDA  260501C00200000",
                "bid": 6.15, "ask": 6.25, "last": 6.20, "mark": 6.20,
                "delta": 0.595, "gamma": 0.033, "theta": -0.260, "vega": 0.120,
                "volatility": 35.0, "strikePrice": 200.0,
                "totalVolume": 1, "openInterest": 1,
                "inTheMoney": True, "settlementType": "P",
                "expirationDate": "2026-05-01", "daysToExpiration": 9,
            }],
        },
    },
    "putExpDateMap": {},
}

_PUT_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 202.50, "change": 2.62, "percentChange": 1.31},
    "callExpDateMap": {},
    "putExpDateMap": {
        "2026-05-01:9": {
            "202.5": [{
                "putCall": "PUT", "symbol": "NVDA  260501P00202500",
                "bid": 4.55, "ask": 4.65, "last": 4.60, "mark": 4.60,
                "delta": -0.489, "gamma": 0.035, "theta": -0.270, "vega": 0.125,
                "volatility": 36.582, "strikePrice": 202.5,
                "totalVolume": 2199, "openInterest": 2940,
                "timeValue": 4.60, "intrinsicValue": 0.0, "inTheMoney": False,
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2026-05-01", "daysToExpiration": 9,
            }],
        },
    },
}


def test_get_greeks_picks_exact_strike_and_side(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="C")
    assert isinstance(result, GreeksResult)
    c = result.envelope["contract"]
    assert c["optionSymbol"] == "NVDA  260501C00202500"
    assert c["strike"] == 202.5
    assert c["side"] == "C"
    assert c["delta"] == 0.510
    # The nearby 200.0 call (delta 0.595) must NOT have been chosen.
    assert c["delta"] != 0.595


def test_get_greeks_picks_put_side(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_PUT_CHAIN_RESP):
        result = get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="P")
    c = result.envelope["contract"]
    assert c["side"] == "P"
    assert c["optionSymbol"] == "NVDA  260501P00202500"
    assert c["delta"] == -0.489


def test_get_greeks_strike_epsilon_tolerates_drift(monkeypatch, tmp_path):
    """A requested strike within 1e-4 of the contract's strike still matches."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = get_greeks(
            "NVDA", strike=202.50001, expiry=date(2026, 5, 1), side="C"
        )
    assert result.envelope["contract"]["strike"] == 202.5


def test_get_greeks_envelope_shape(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        result = get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="C")
    env = result.envelope
    assert set(env.keys()) == {
        "underlyingSymbol",
        "expiry",
        "dte",
        "underlying",
        "contract",
    }
    assert env["underlyingSymbol"] == "NVDA"
    assert env["expiry"] == "2026-05-01"
    assert env["dte"] == 9
    assert env["underlying"]["last"] == 202.5
    assert env["underlying"]["netChange"] == 2.62
    assert env["underlying"]["pctChange"] == 1.31


def test_get_greeks_contract_not_found(monkeypatch, tmp_path):
    """No matching strike/side raises ContractNotFound with request fields."""
    _prep(monkeypatch, tmp_path)
    no_strike_resp = {
        "underlying": _CHAIN_RESP["underlying"],
        "callExpDateMap": {
            "2026-05-01:9": {
                "200.0": _CHAIN_RESP["callExpDateMap"]["2026-05-01:9"]["200.0"],
            },
        },
        "putExpDateMap": {},
    }
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=no_strike_resp):
        with pytest.raises(ContractNotFound) as exc:
            get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="C")
    err = exc.value
    assert err.underlying == "NVDA"
    assert err.expiry == date(2026, 5, 1)
    assert err.strike == 202.5
    assert err.contract_type == "CALL"


def test_get_greeks_contract_not_found_put_type(monkeypatch, tmp_path):
    """A PUT request with only a call payload reports contract_type PUT."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_CHAIN_RESP):
        with pytest.raises(ContractNotFound) as exc:
            get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="P")
    assert exc.value.contract_type == "PUT"


def test_get_greeks_no_config_raises_not_configured(monkeypatch, tmp_path):
    """With an empty HOME (no config on disk) get_greeks raises NotConfigured."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(NotConfigured):
        get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="C")


def test_get_greeks_no_session_raises_not_authenticated(monkeypatch, tmp_path):
    """Config present but no session file -> the auth error propagates
    through the service boundary unchanged."""
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
        get_greeks("NVDA", strike=202.5, expiry=date(2026, 5, 1), side="C")
