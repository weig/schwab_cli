import json
from datetime import datetime, timezone

import pytest

from schwab_cli.output.format import Format
from schwab_cli.output.history import render_history, shape_envelope


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# A deterministic 4-candle daily sample:
#   previousClose = 100.00
#   2024-04-22 close=101 (+1)     up
#   2024-04-23 close=100 (-1)     down
#   2024-04-24 close=100 (0)      flat
#   2024-04-25 close=NaN          missing
_CANDLES_DAILY = [
    {
        "datetime": _ms(datetime(2024, 4, 22, 13, 30, tzinfo=timezone.utc)),
        # 09:30 NY = 13:30 UTC (EDT, UTC-4)
        "open": 100.50, "high": 101.90, "low": 100.10,
        "close": 101.00, "volume": 1_000_000,
    },
    {
        "datetime": _ms(datetime(2024, 4, 23, 13, 30, tzinfo=timezone.utc)),
        "open": 101.00, "high": 101.50, "low":  99.80,
        "close": 100.00, "volume": 1_200_000,
    },
    {
        "datetime": _ms(datetime(2024, 4, 24, 13, 30, tzinfo=timezone.utc)),
        "open": 100.00, "high": 100.50, "low":  99.80,
        "close": 100.00, "volume":   800_000,
    },
    {
        "datetime": _ms(datetime(2024, 4, 25, 13, 30, tzinfo=timezone.utc)),
        "open":  99.50, "high": 100.30, "low":  99.00,
        "close": float("nan"), "volume":   500_000,
    },
]


_RAW_DAILY = {
    "symbol": "NVDA",
    "empty": False,
    "previousClose": 100.00,
    "previousCloseDate": _ms(datetime(2024, 4, 19, 20, 0, tzinfo=timezone.utc)),
    "candles": _CANDLES_DAILY,
}


# ---------------------------------------------------------------------------
# shape_envelope
# ---------------------------------------------------------------------------

def test_shape_envelope_basic_fields():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    assert env["symbol"] == "NVDA"
    assert env["interval"] == "1day"
    assert env["previousClose"] == 100.00
    assert len(env["candles"]) == 4


def test_shape_envelope_daily_datetime_is_date_only():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    # 2024-04-22 in NY — date-only formatting for daily+.
    assert env["candles"][0]["datetime"] == "2024-04-22"


def test_shape_envelope_intraday_datetime_is_full():
    raw = {**_RAW_DAILY, "candles": _CANDLES_DAILY[:1]}
    env = shape_envelope(raw, interval="15min")
    # 13:30 UTC → 09:30 NY.
    assert env["candles"][0]["datetime"] == "2024-04-22 09:30:00"


def test_shape_envelope_weekly_is_date_only():
    env = shape_envelope(_RAW_DAILY, interval="1wk")
    assert env["candles"][0]["datetime"] == "2024-04-22"


def test_shape_envelope_monthly_is_date_only():
    env = shape_envelope(_RAW_DAILY, interval="1mo")
    assert env["candles"][0]["datetime"] == "2024-04-22"


def test_shape_envelope_change_row_0_from_previous_close():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    c0 = env["candles"][0]
    # prior = 100.00, close = 101.00, change = 1.00, changePct = 1.00
    assert c0["change"] == pytest.approx(1.00)
    assert c0["changePct"] == pytest.approx(1.00)


def test_shape_envelope_change_row_n_from_prior_close():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    c1 = env["candles"][1]
    # prior = 101.00, close = 100.00, change = -1.00, changePct = -1/101 * 100
    assert c1["change"] == pytest.approx(-1.00)
    assert c1["changePct"] == pytest.approx(-100 / 101)


def test_shape_envelope_flat_change_is_zero():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    c2 = env["candles"][2]
    assert c2["change"] == pytest.approx(0.0)
    assert c2["changePct"] == pytest.approx(0.0)


def test_shape_envelope_nan_close_null_everything():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    c3 = env["candles"][3]
    # close was NaN → shaped to None; change/changePct also None.
    assert c3["close"] is None
    assert c3["change"] is None
    assert c3["changePct"] is None


def test_shape_envelope_no_previous_close_row_0_is_null():
    raw = {**_RAW_DAILY}
    raw.pop("previousClose", None)
    raw["candles"] = _CANDLES_DAILY[:1]
    env = shape_envelope(raw, interval="1day")
    c0 = env["candles"][0]
    assert c0["change"] is None
    assert c0["changePct"] is None
    assert env["previousClose"] is None


def test_shape_envelope_from_and_to_in_ny_iso():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    # First candle 09:30 NY on 2024-04-22 → from should reflect that or earlier.
    # With only candle datetimes, "from"/"to" is candles[0] / candles[-1].
    assert env["from"] == "2024-04-22T09:30:00-04:00"
    assert env["to"] == "2024-04-25T09:30:00-04:00"


def test_shape_envelope_empty_candles():
    raw = {"symbol": "XYZZZ", "empty": True, "candles": []}
    env = shape_envelope(raw, interval="1day")
    assert env["candles"] == []
    assert env["from"] is None
    assert env["to"] is None


# ---------------------------------------------------------------------------
# render_history — JSON
# ---------------------------------------------------------------------------

def test_render_json_roundtrip():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.JSON)
    # Must be plain JSON, no ANSI.
    assert "\x1b[" not in out
    data = json.loads(out)
    assert data["symbol"] == "NVDA"
    assert data["interval"] == "1day"
    assert len(data["candles"]) == 4
    # NaN close must serialize as JSON null.
    assert data["candles"][3]["close"] is None


def test_render_json_field_set():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    data = json.loads(render_history(env, fmt=Format.JSON))
    for key in ("datetime", "open", "high", "low", "close",
                "volume", "change", "changePct"):
        assert key in data["candles"][0]


# ---------------------------------------------------------------------------
# render_history — HUMAN
# ---------------------------------------------------------------------------

def test_render_human_header_line():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    # Header mentions symbol, interval, and candle count.
    assert "NVDA" in out
    assert "1day" in out
    assert "4 candles" in out


def test_render_human_has_all_eight_columns():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    for col in ("Date", "Open", "High", "Low", "Close", "Change", "Change%", "Volume"):
        assert col in out


def test_render_human_has_green_and_red_ansi():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    # 32 = green foreground, 31 = red — present when we have up+down days.
    assert "\x1b[32m" in out
    assert "\x1b[31m" in out


def test_render_human_em_dash_for_missing():
    raw = {**_RAW_DAILY}
    raw.pop("previousClose", None)
    raw["candles"] = _CANDLES_DAILY[:1]
    env = shape_envelope(raw, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    assert "—" in out


def test_render_human_volume_has_thousands_separator():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    assert "1,000,000" in out


# ---------------------------------------------------------------------------
# render_history — MD
# ---------------------------------------------------------------------------

def test_render_md_heading():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.MD)
    assert out.startswith("# NVDA — 1day")


def test_render_md_no_ansi():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.MD)
    assert "\x1b[" not in out


def test_render_md_has_table_separator():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.MD)
    # A GitHub-flavored table separator row.
    assert "|---" in out or "| ---" in out


def test_render_md_has_previous_close_and_count():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.MD)
    assert "Previous close" in out
    assert "Candles:" in out and "4" in out


def test_render_md_columns():
    env = shape_envelope(_RAW_DAILY, interval="1day")
    out = render_history(env, fmt=Format.MD)
    for col in ("Date", "Open", "High", "Low", "Close", "Change", "Change%", "Volume"):
        assert col in out


# ---------------------------------------------------------------------------
# Empty-envelope rendering
# ---------------------------------------------------------------------------

def test_render_empty_human_does_not_crash():
    raw = {"symbol": "XYZZZ", "empty": True, "candles": []}
    env = shape_envelope(raw, interval="1day")
    out = render_history(env, fmt=Format.HUMAN)
    assert "XYZZZ" in out


def test_render_empty_json_has_empty_candles():
    raw = {"symbol": "XYZZZ", "empty": True, "candles": []}
    env = shape_envelope(raw, interval="1day")
    out = render_history(env, fmt=Format.JSON)
    data = json.loads(out)
    assert data["candles"] == []


def test_render_empty_md_does_not_crash():
    raw = {"symbol": "XYZZZ", "empty": True, "candles": []}
    env = shape_envelope(raw, interval="1day")
    out = render_history(env, fmt=Format.MD)
    assert "XYZZZ" in out
