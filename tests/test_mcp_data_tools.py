"""Tests for the Tier-A MCP data tools (vol/skew/greeks/history/
dividends/fundamentals). Services are patched — no real network."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from schwab_cli.mcp_server.app import SchwabMcpServer
from schwab_cli.mcp_server.logbook import LogBook


class _FakeSession:
    access_token = "atok"
    refresh_token = "rtok"
    expires_at = 9_000_000_000
    refresh_token_expires_at = 9_000_000_000


class _FakeClient:
    @property
    def session(self):
        return _FakeSession()


def _server() -> SchwabMcpServer:
    return SchwabMcpServer(_FakeClient(), LogBook(stream=io.StringIO()))


def _call(coro):
    return asyncio.run(coro)


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


# ---- get_vol ---------------------------------------------------------------


def test_get_vol_happy_upcases_and_no_record():
    captured = {}

    def fake_get_vol(self, symbol, **kw):
        captured["symbol"] = symbol
        captured["kw"] = kw
        return SimpleNamespace(envelope={"symbol": symbol, "atm_iv": 0.31})

    s = _server()
    with patch("schwab_cli.service.vol.VolService.get_vol", fake_get_vol):
        out = _text(_call(s._tool_get_vol({"symbol": "nvda"})))
    assert captured["symbol"] == "NVDA"
    assert captured["kw"]["no_record"] is True  # MCP read must not record
    assert json.loads(out)["atm_iv"] == 0.31


def test_get_vol_requires_symbol():
    s = _server()
    assert "symbol is required" in _text(_call(s._tool_get_vol({})))


# ---- get_skew --------------------------------------------------------------


def test_get_skew_happy():
    s = _server()
    with patch(
        "schwab_cli.service.skew.SkewService.get_skew_l1",
        return_value=SimpleNamespace(metrics={"rr25": -1.2}),
    ):
        out = _text(_call(s._tool_get_skew(
            {"symbol": "spy", "expiry": "2026-06-19"}
        )))
    assert json.loads(out)["rr25"] == -1.2


def test_get_skew_bad_expiry():
    s = _server()
    out = _text(_call(s._tool_get_skew({"symbol": "SPY", "expiry": "nope"})))
    assert "invalid expiry" in out


def test_get_skew_missing_args():
    s = _server()
    assert "required" in _text(_call(s._tool_get_skew({"symbol": "SPY"})))


# ---- get_greeks ------------------------------------------------------------


def test_get_greeks_happy():
    captured = {}

    def fake(self, underlying, **kw):
        captured["underlying"] = underlying
        captured["kw"] = kw
        return SimpleNamespace(envelope={"delta": 0.5})

    s = _server()
    with patch("schwab_cli.service.greeks.GreeksService.get_greeks", fake):
        out = _text(_call(s._tool_get_greeks({
            "underlying": "aapl", "strike": 200, "expiry": "2026-06-19",
            "side": "c",
        })))
    assert captured["underlying"] == "AAPL"
    assert captured["kw"]["side"] == "C"
    assert captured["kw"]["strike"] == 200.0
    assert json.loads(out)["delta"] == 0.5


def test_get_greeks_bad_side():
    s = _server()
    out = _text(_call(s._tool_get_greeks({
        "underlying": "AAPL", "strike": 200, "expiry": "2026-06-19", "side": "X",
    })))
    assert "side must be" in out


# ---- get_history -----------------------------------------------------------


def test_get_history_happy_defaults():
    captured = {}

    def fake(self, symbol, **kw):
        captured["symbol"] = symbol
        captured["kw"] = kw
        return SimpleNamespace(envelope={"candles": [1, 2, 3]})

    s = _server()
    with patch("schwab_cli.service.history.HistoryService.get_history", fake):
        out = _text(_call(s._tool_get_history({"symbol": "qqq"})))
    assert captured["symbol"] == "QQQ"
    assert captured["kw"]["frequency_type"] == "daily"
    assert json.loads(out)["candles"] == [1, 2, 3]


def test_get_history_bad_interval():
    s = _server()
    out = _text(_call(s._tool_get_history(
        {"symbol": "QQQ", "interval": "bogus"}
    )))
    assert "invalid interval" in out


# ---- get_dividends / get_fundamentals --------------------------------------


def test_get_dividends_happy():
    s = _server()
    with patch(
        "schwab_cli.service.dividends.DividendsService.get_dividends",
        return_value=SimpleNamespace(payload={"NVDA": {"divYield": 0.01}}),
    ):
        out = _text(_call(s._tool_get_dividends({"symbols": ["nvda"]})))
    assert json.loads(out)["NVDA"]["divYield"] == 0.01


def test_get_fundamentals_happy():
    s = _server()
    with patch(
        "schwab_cli.service.fundamentals.FundamentalsService.get_fundamentals",
        return_value=SimpleNamespace(payload={"AAPL": {"peRatio": 30}}),
    ):
        out = _text(_call(s._tool_get_fundamentals({"symbols": ["aapl"]})))
    assert json.loads(out)["AAPL"]["peRatio"] == 30


def test_data_tools_reject_empty_symbol_list():
    s = _server()
    assert "empty" in _text(_call(s._tool_get_dividends({"symbols": []})))
    assert "list of strings" in _text(
        _call(s._tool_get_fundamentals({"symbols": "AAPL"}))
    )


# ---- _dispatch: schwab/auth errors become text, never raise ----------------


def test_dispatch_converts_auth_error_to_text():
    from schwab_cli.service.auth import NotAuthenticated

    s = _server()
    with patch(
        "schwab_cli.service.vol.VolService.get_vol",
        side_effect=NotAuthenticated("no session"),
    ):
        out = _text(_call(s._dispatch("get_vol", {"symbol": "NVDA"})))
    assert "schwab error" in out and "NotAuthenticated" in out


def test_dispatch_unknown_tool():
    s = _server()
    assert "unknown tool" in _text(_call(s._dispatch("nope", {})))


# ---- bad-type coercion → clean message (not "internal error") --------------


def test_bad_int_arg_returns_clean_message_via_dispatch():
    s = _server()
    with patch(
        "schwab_cli.service.vol.VolService.get_vol",
        return_value=SimpleNamespace(envelope={}),
    ):
        out = _text(_call(s._dispatch(
            "get_vol", {"symbol": "NVDA", "hv_window": "fast"}
        )))
    assert "hv_window must be an integer" in out
    assert "internal error" not in out


def test_bad_float_strike_returns_clean_message_via_dispatch():
    s = _server()
    out = _text(_call(s._dispatch("get_greeks", {
        "underlying": "AAPL", "strike": "atm",
        "expiry": "2026-06-19", "side": "C",
    })))
    assert "strike must be a number" in out


def test_int_arg_keeps_explicit_zero():
    # 0 must be forwarded, not silently replaced by the default.
    captured = {}

    def fake_get_vol(self, symbol, **kw):
        captured["kw"] = kw
        return SimpleNamespace(envelope={})

    s = _server()
    with patch("schwab_cli.service.vol.VolService.get_vol", fake_get_vol):
        _call(s._tool_get_vol({"symbol": "NVDA", "ivp_lookback": 0}))
    assert captured["kw"]["ivp_lookback"] == 0
