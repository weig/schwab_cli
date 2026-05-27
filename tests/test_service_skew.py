"""Unit tests for the Layer-2 ``service.skew`` functions.

These exercise the service in isolation from the command shim. The stable
``schwab_cli.api.chains.get_chain`` seam is mocked so no real HTTP happens,
and a future-dated session keeps ``service.auth.get_session`` from
attempting an ``oauth.refresh``.

Coverage per mode:
  - happy-path metrics (the shape the renderers consume);
  - partial-failure tolerance (one fetch fails -> others still render);
  - all-fail -> ``NoSkewData``;
  - ``NotConfigured`` / ``NotAuthenticated`` propagate through the boundary.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.skew import (
    DiscoveryError,
    NoSkewData,
    SkewService,
)
from schwab_cli.service.types import SkewResult
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

_GET_CHAIN = "schwab_cli.api.chains.get_chain"


class _RecordingSink:
    """Capture the service's skip notices (the old `on_skip` callback).

    ``SkewService`` emits partial-failure skip lines via ``self._out.info``;
    the recording sink appends them so the assertions that used to inspect
    the ``on_skip`` callback's captures keep working unchanged.
    """

    def __init__(self, sink_list: list[str]) -> None:
        self._sink = sink_list

    def info(self, message: str) -> None:
        self._sink.append(message)

    def progress(self, message: str) -> None:  # never used by skew
        self._sink.append(message)



def _prep(monkeypatch, tmp_path):
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
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


def _future(days_out: int) -> tuple[str, date]:
    d = date.today() + timedelta(days=days_out)
    return d.isoformat(), d


def _chain_resp(iso_expiry: str, *, symbol: str = "AMZN", dte: int = 30) -> dict:
    """Schwab-shaped chain dense enough for compute_skew to land ATM, 25Δ,
    10Δ and a slope. Mirrors the command-test fixture so the metrics are
    well-formed."""
    calls = {
        245.0: (0.75, 0.65),
        250.0: (0.60, 0.63),
        255.0: (0.53, 0.620),
        257.5: (0.50, 0.6162),
        260.0: (0.46, 0.612),
        265.0: (0.38, 0.605),
        270.0: (0.30, 0.600),
        272.5: (0.26, 0.5951),
        275.0: (0.22, 0.597),
        280.0: (0.17, 0.6002),
    }
    puts = {
        232.5: (-0.16, 0.6380),
        240.0: (-0.25, 0.6280),
        250.0: (-0.40, 0.622),
        255.0: (-0.47, 0.619),
        257.5: (-0.50, 0.6158),
    }

    def _row(put_call: str, strike: float, delta: float, iv: float) -> dict:
        return {
            "symbol": f"{symbol}  XXXXXX{strike:08.0f}",
            "putCall": put_call,
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,
            "bid": 1.0, "ask": 1.05, "last": 1.02,
            "totalVolume": 100, "openInterest": 100,
            "expirationDate": iso_expiry,
            "daysToExpiration": dte,
        }

    return {
        "symbol": symbol,
        "underlying": {"last": 255.36, "change": 0.0, "percentChange": 0.0},
        "callExpDateMap": {
            f"{iso_expiry}:{dte}": {
                f"{s:.1f}": [_row("CALL", s, d, iv)] for s, (d, iv) in calls.items()
            }
        },
        "putExpDateMap": {
            f"{iso_expiry}:{dte}": {
                f"{s:.1f}": [_row("PUT", s, d, iv)] for s, (d, iv) in puts.items()
            }
        },
    }


def _disc_resp(pairs: list[tuple[str, int]]) -> dict:
    """Minimal discovery payload — only the expDateMap keys matter."""
    return {
        "symbol": "AMZN",
        "callExpDateMap": {f"{iso}:{dte}": {} for iso, dte in pairs},
        "putExpDateMap": {},
    }


# ---- L1 ----------------------------------------------------------------


def test_l1_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso, exp = _future(30)
    with patch(_GET_CHAIN, return_value=_chain_resp(iso)):
        result = SkewService().get_skew_l1("AMZN", exp, strikes=40)
    assert isinstance(result, SkewResult)
    assert result.symbol is None
    m = result.metrics
    assert m["symbol"] == "AMZN"
    assert m["spot"] == 255.36
    assert m["d25"]["rr"] == pytest.approx(3.29, abs=0.01)


def test_l1_empty_envelope_raises_no_skew_data(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso, exp = _future(30)
    empty = {
        "symbol": "AMZN",
        "underlying": {"last": 1.0, "change": 0, "percentChange": 0},
        "callExpDateMap": {},
        "putExpDateMap": {},
    }
    with patch(_GET_CHAIN, return_value=empty):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_l1("AMZN", exp, strikes=40)
    assert "No contracts for AMZN" in str(exc.value)


def test_l1_api_error_propagates(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso, exp = _future(30)
    with patch(_GET_CHAIN, side_effect=ApiError("503")):
        with pytest.raises(ApiError):
            SkewService().get_skew_l1("AMZN", exp, strikes=40)


def test_l1_no_config_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    _iso, exp = _future(30)
    with pytest.raises(NotConfigured):
        SkewService().get_skew_l1("AMZN", exp, strikes=40)


def test_l1_no_session_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(
        Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    )
    _iso, exp = _future(30)
    with pytest.raises(NotAuthenticated):
        SkewService().get_skew_l1("AMZN", exp, strikes=40)


# ---- L2 --term ---------------------------------------------------------


def test_term_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso1, e1 = _future(10)
    iso2, e2 = _future(40)

    def _side(client, symbol, **kwargs):
        fd = kwargs["from_date"].isoformat()
        return _chain_resp(iso1, dte=10) if fd == iso1 else _chain_resp(iso2, dte=40)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService().get_skew_term("AMZN", [e1, e2], strikes=40)
    assert result.symbol == "AMZN"
    assert isinstance(result.metrics, list)
    assert [m["dte"] for m in result.metrics] == [10, 40]


def test_term_partial_failure_continues(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso1, e1 = _future(10)
    _iso2, e2 = _future(40)
    skips: list[str] = []
    n = {"calls": 0}

    def _side(client, symbol, **kwargs):
        n["calls"] += 1
        if n["calls"] == 2:
            raise ApiError("timeout")
        return _chain_resp(iso1, dte=10)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService(out=_RecordingSink(skips)).get_skew_term(
            "AMZN", [e1, e2], strikes=40
        )
    assert len(result.metrics) == 1
    assert len(skips) == 1
    assert "skip AMZN" in skips[0]


def test_term_all_fail_raises_no_skew_data(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _iso1, e1 = _future(10)
    _iso2, e2 = _future(40)
    with patch(_GET_CHAIN, side_effect=ApiError("down")):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_term("AMZN", [e1, e2], strikes=40)
    assert "No usable chains for AMZN across 2 expiries" in str(exc.value)


def test_term_session_expired_skipped(monkeypatch, tmp_path):
    """SessionExpired on a per-expiry fetch is treated as a skip, not fatal."""
    _prep(monkeypatch, tmp_path)
    iso1, e1 = _future(10)
    _iso2, e2 = _future(40)
    n = {"calls": 0}

    def _side(client, symbol, **kwargs):
        n["calls"] += 1
        if n["calls"] == 1:
            raise SessionExpired("stale")
        return _chain_resp(iso1, dte=10)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService().get_skew_term("AMZN", [e1, e2], strikes=40)
    assert len(result.metrics) == 1


def test_term_no_config_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    _iso, exp = _future(30)
    with pytest.raises(NotConfigured):
        SkewService().get_skew_term("AMZN", [exp], strikes=40)


# ---- L2 --dtes ---------------------------------------------------------


def test_dtes_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    today = date.today()
    iso10 = (today + timedelta(days=10)).isoformat()
    iso40 = (today + timedelta(days=40)).isoformat()
    disc = _disc_resp([(iso10, 10), (iso40, 40)])

    def _side(client, symbol, **kwargs):
        if kwargs.get("strike_count") == 2:
            return disc
        fd = kwargs["from_date"].isoformat()
        return _chain_resp(iso10, dte=10) if fd == iso10 else _chain_resp(iso40, dte=40)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService().get_skew_dtes("AMZN", [10, 40], strikes=40)
    assert result.symbol == "AMZN"
    assert [m["dte"] for m in result.metrics] == [10, 40]


def test_dtes_no_expiries_raises(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(_GET_CHAIN, return_value=_disc_resp([])):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_dtes("AMZN", [30], strikes=40)
    assert "No expiries discoverable for AMZN" in str(exc.value)


def test_dtes_discovery_api_error_propagates(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(_GET_CHAIN, side_effect=ApiError("503")):
        with pytest.raises(ApiError):
            SkewService().get_skew_dtes("AMZN", [30], strikes=40)


def test_dtes_partial_failure_continues(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    today = date.today()
    iso10 = (today + timedelta(days=10)).isoformat()
    iso40 = (today + timedelta(days=40)).isoformat()
    disc = _disc_resp([(iso10, 10), (iso40, 40)])
    skips: list[str] = []

    def _side(client, symbol, **kwargs):
        if kwargs.get("strike_count") == 2:
            return disc
        fd = kwargs["from_date"].isoformat()
        if fd == iso40:
            raise ApiError("timeout")
        return _chain_resp(iso10, dte=10)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService(out=_RecordingSink(skips)).get_skew_dtes(
            "AMZN", [10, 40], strikes=40
        )
    assert len(result.metrics) == 1
    assert len(skips) == 1


def test_dtes_all_fetch_fail_raises(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    today = date.today()
    iso10 = (today + timedelta(days=10)).isoformat()
    disc = _disc_resp([(iso10, 10)])

    def _side(client, symbol, **kwargs):
        if kwargs.get("strike_count") == 2:
            return disc
        raise ApiError("timeout")

    with patch(_GET_CHAIN, side_effect=_side):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_dtes("AMZN", [10], strikes=40)
    assert "No usable chains for AMZN at target DTEs" in str(exc.value)


# ---- L3 --cross --------------------------------------------------------


def test_cross_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso, exp = _future(30)

    def _side(client, symbol, **kwargs):
        return _chain_resp(iso, symbol=symbol)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService().get_skew_cross(exp, ["AAPL", "NVDA"], strikes=40)
    assert result.symbol is None
    assert isinstance(result.metrics, list)
    assert len(result.metrics) == 2
    assert {m["symbol"] for m in result.metrics} == {"AAPL", "NVDA"}


def test_cross_partial_failure_continues(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    iso, exp = _future(30)
    skips: list[str] = []

    def _side(client, symbol, **kwargs):
        if symbol.upper() == "NVDA":
            raise ApiError("timeout")
        return _chain_resp(iso, symbol=symbol)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService(out=_RecordingSink(skips)).get_skew_cross(
            exp, ["AAPL", "NVDA"], strikes=40
        )
    assert len(result.metrics) == 1
    assert result.metrics[0]["symbol"] == "AAPL"
    assert len(skips) == 1


def test_cross_all_fail_raises(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _iso, exp = _future(30)
    with patch(_GET_CHAIN, side_effect=ApiError("down")):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_cross(exp, ["AAPL", "NVDA"], strikes=40)
    assert "No usable chains across 2 symbols" in str(exc.value)


# ---- L3 --cross --dtes -------------------------------------------------


def test_cross_dtes_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    today = date.today()
    iso30 = (today + timedelta(days=30)).isoformat()

    def _side(client, symbol, **kwargs):
        if kwargs.get("strike_count") == 2:
            return _disc_resp([(iso30, 30)])
        return _chain_resp(iso30, symbol=symbol)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService().get_skew_cross_dtes(30, ["AAPL", "NVDA"], strikes=40)
    assert len(result.metrics) == 2
    assert {m["symbol"] for m in result.metrics} == {"AAPL", "NVDA"}


def test_cross_dtes_discovery_empty_skipped(monkeypatch, tmp_path):
    """A symbol with no discoverable expiries is skipped, not fatal."""
    _prep(monkeypatch, tmp_path)
    today = date.today()
    iso30 = (today + timedelta(days=30)).isoformat()
    skips: list[str] = []

    def _side(client, symbol, **kwargs):
        if kwargs.get("strike_count") == 2:
            if symbol.upper() == "NVDA":
                return _disc_resp([])  # no expiries for NVDA
            return _disc_resp([(iso30, 30)])
        return _chain_resp(iso30, symbol=symbol)

    with patch(_GET_CHAIN, side_effect=_side):
        result = SkewService(out=_RecordingSink(skips)).get_skew_cross_dtes(
            30, ["AAPL", "NVDA"], strikes=40
        )
    assert len(result.metrics) == 1
    assert result.metrics[0]["symbol"] == "AAPL"
    assert any("no expiries discoverable for NVDA" in s for s in skips)


def test_cross_dtes_all_fail_raises(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(_GET_CHAIN, return_value=_disc_resp([])):
        with pytest.raises(NoSkewData) as exc:
            SkewService().get_skew_cross_dtes(30, ["AAPL", "NVDA"], strikes=40)
    assert "No usable chains across 2 symbols at ~30 DTE" in str(exc.value)


def test_cross_dtes_discovery_error_names_symbol(monkeypatch, tmp_path):
    """A discovery API failure is fatal and the message names the symbol —
    matching the single-symbol `dtes` mode (regression guard)."""
    _prep(monkeypatch, tmp_path)
    with patch(_GET_CHAIN, side_effect=ApiError("500 boom")):
        with pytest.raises(DiscoveryError) as exc:
            SkewService().get_skew_cross_dtes(30, ["NVDA", "AAPL"], strikes=40)
    assert str(exc.value) == "chain discovery failed for NVDA: 500 boom"
