from datetime import date

import httpx
import pytest
import respx

from schwab_cli.api.chains import flatten_chain, get_chain
from schwab_cli.api.client import SchwabClient
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    s = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, s)


_SAMPLE = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35},
    "callExpDateMap": {},
    "putExpDateMap": {},
}


@respx.mock
def test_get_chain_default_params():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", from_date=date(2027, 1, 15), to_date=date(2027, 1, 15))
    params = route.calls.last.request.url.params
    assert params["symbol"] == "NVDA"
    assert params["contractType"] == "ALL"
    assert params["strategy"] == "SINGLE"
    assert params["includeUnderlyingQuote"] == "true"
    # default strike_count=10 → Schwab strikeCount=5 (per-side)
    assert params["strikeCount"] == "5"
    assert params["fromDate"] == "2027-01-15"
    assert params["toDate"] == "2027-01-15"
    assert "strike" not in params


@respx.mock
def test_get_chain_strike_count_rounds_up_for_odd():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike_count=5)
    # ceil(5/2) = 3
    assert route.calls.last.request.url.params["strikeCount"] == "3"


@respx.mock
def test_get_chain_with_explicit_strike():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike=250.0)
    assert route.calls.last.request.url.params["strike"] == "250.0"


def test_get_chain_rejects_zero_or_negative_strike_count():
    with pytest.raises(ValueError):
        get_chain(_client(), "NVDA", strike_count=0)
    with pytest.raises(ValueError):
        get_chain(_client(), "NVDA", strike_count=-5)


@respx.mock
def test_get_chain_contract_type_forwarded():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", contract_type="PUT")
    assert route.calls.last.request.url.params["contractType"] == "PUT"


@respx.mock
def test_get_chain_returns_response_dict():
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE


@respx.mock
def test_get_chain_empty_response_passthrough():
    empty = {"symbol": "XYZZZ", "status": "FAILED",
             "callExpDateMap": {}, "putExpDateMap": {}}
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=empty),
    )
    result = get_chain(_client(), "XYZZZ")
    assert result["status"] == "FAILED"


@respx.mock
def test_get_chain_401_then_refresh_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        side_effect=[
            httpx.Response(401, json={}),
            httpx.Response(200, json=_SAMPLE),
        ],
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new_at", "refresh_token": "new_rt",
            "expires_in": 1800,
        }),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE


# ---- flatten_chain: IV sentinel / non-positive handling ----------------
#
# Schwab returns -999.0 in the `volatility` field on market holidays and
# illiquid snapshots.  Before this fix, -999.0 / 100 = -9.99 was stored
# as a real IV value, polluting the IVP/IVR range.
#
# Contract: any volatility value that is non-positive (including the
# -999.0 sentinel, 0, and other negatives) MUST produce iv=None.
# Only a strictly positive numeric value maps to iv_pct / 100.0.


def _make_raw(volatility) -> dict:
    """Build a minimal raw chain with a single call strike at the given
    `volatility` value (mirrors the shape the existing tests use)."""
    return {
        "callExpDateMap": {
            "2026-06-20:22": {
                "200.0": [{
                    "strikePrice": 200.0,
                    "volatility": volatility,
                    "delta": 0.5,
                    "totalVolume": 100,
                    "openInterest": 50,
                }]
            }
        },
        "putExpDateMap": {},
    }


def _first_iv(raw: dict):
    """Return the iv field from the first flattened contract."""
    _expiries, flat = flatten_chain(raw)
    assert flat, "expected at least one contract from flatten_chain"
    return flat[0]["iv"]


def test_flatten_chain_sentinel_minus_999_yields_none():
    # -999.0 is Schwab's "IV unavailable" sentinel — primary bug case.
    # Current code: -999.0 / 100 = -9.99 (non-None). Must become None.
    assert _first_iv(_make_raw(-999.0)) is None


def test_flatten_chain_zero_volatility_yields_none():
    # Zero is non-positive: no meaningful IV.
    assert _first_iv(_make_raw(0)) is None


def test_flatten_chain_negative_non_sentinel_yields_none():
    # Any other negative value is also implausible.
    assert _first_iv(_make_raw(-5.0)) is None


def test_flatten_chain_valid_positive_volatility_divided_by_100():
    # A well-formed positive volatility (e.g. 34.66%) is still
    # normalised to decimal form 0.3466.
    iv = _first_iv(_make_raw(34.66))
    assert iv == pytest.approx(0.3466)


def test_flatten_chain_missing_volatility_key_yields_none():
    # Field absent from the row dict entirely.
    raw = {
        "callExpDateMap": {
            "2026-06-20:22": {
                "200.0": [{
                    "strikePrice": 200.0,
                    # "volatility" key intentionally omitted
                    "delta": 0.5,
                    "totalVolume": 100,
                    "openInterest": 50,
                }]
            }
        },
        "putExpDateMap": {},
    }
    assert _first_iv(raw) is None


def test_flatten_chain_none_volatility_yields_none():
    # Explicit None value.
    assert _first_iv(_make_raw(None)) is None


def test_flatten_chain_nan_string_volatility_yields_none():
    # Non-numeric string (e.g. "NaN") must not crash and must yield None.
    assert _first_iv(_make_raw("NaN")) is None


def test_flatten_chain_infinite_volatility_yields_none():
    # Non-finite values (inf/nan) are not valid IVs and must not propagate.
    assert _first_iv(_make_raw(float("inf"))) is None
    assert _first_iv(_make_raw(float("nan"))) is None
