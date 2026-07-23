"""Tests for the focus-tier full-chain capture (schema v9)."""
from __future__ import annotations

import pytest

from schwab_cli.screener.capture import capture_full_chain, extract_full_chain
from schwab_cli.storage import screener as store
from schwab_cli.storage.vol_history import _SCHEMA_VERSION, connect


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def _contract(strike, delta, iv_pct=32.5, bid=1.0, ask=1.2, gamma=0.01,
              oi=500, vol=50):
    return {"strikePrice": strike, "delta": delta, "volatility": iv_pct,
            "bid": bid, "ask": ask, "last": (bid + ask) / 2, "gamma": gamma,
            "theta": -0.05, "vega": 0.12, "openInterest": oi,
            "totalVolume": vol}


def _chain(spot=540.0) -> dict:
    return {
        "underlying": {"last": spot},
        "callExpDateMap": {
            "2026-08-21:31": {"540": [_contract(540, 0.52)],
                              "560": [_contract(560, 0.25)]},
            "2026-09-18:59": {"540": [_contract(540, 0.55)]},
        },
        "putExpDateMap": {
            "2026-08-21:31": {"540": [_contract(540, -0.48)],
                              "500": [_contract(500, -0.20)]},
        },
    }


def test_schema_v9_table_and_version(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "option_chain_snapshots" in tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] \
        == _SCHEMA_VERSION


def test_extract_full_chain_both_sides_all_expiries():
    contracts = extract_full_chain(_chain())
    assert len(contracts) == 5                       # 3 calls + 2 puts
    sides = {(c["side"], c["expiry"], c["strike"]) for c in contracts}
    assert ("C", "2026-09-18", 540.0) in sides       # far expiry kept
    assert ("P", "2026-08-21", 500.0) in sides
    c = next(x for x in contracts if x["side"] == "C" and x["strike"] == 540
             and x["expiry"] == "2026-08-21")
    assert c["iv"] == pytest.approx(0.325)           # percent → decimal
    assert c["gamma"] == 0.01 and c["open_interest"] == 500


def test_capture_persists_and_is_idempotent(conn):
    n = capture_full_chain(conn, snapshot_date="2026-07-23", symbol="SPY",
                           raw=_chain(), now_ms=1)
    assert n == 5
    # Re-capture with a refreshed quote → upsert, no duplication.
    ch = _chain()
    ch["callExpDateMap"]["2026-08-21:31"]["540"][0]["bid"] = 9.9
    capture_full_chain(conn, snapshot_date="2026-07-23", symbol="SPY",
                       raw=ch, now_ms=2)
    rows = store.read_chain_snapshot(conn, snapshot_date="2026-07-23",
                                     symbol="SPY")
    assert len(rows) == 5
    updated = next(r for r in rows if r["side"] == "C" and r["strike"] == 540
                   and r["expiry"] == "2026-08-21")
    assert updated["bid"] == 9.9
    assert updated["underlying_last"] == 540.0


def test_capture_empty_chain_writes_nothing(conn):
    n = capture_full_chain(conn, snapshot_date="2026-07-23", symbol="X",
                           raw={"underlying": {"last": 1.0}}, now_ms=1)
    assert n == 0


def test_focus_config_default_includes_vehicles():
    from schwab_cli.dataset.config import DEFAULT_CONFIG
    focus = DEFAULT_CONFIG["focus_chain"]
    assert "$XSP" in focus and "SPY" in focus and "TSLA" in focus
    assert len(focus) <= 25          # focus tier stays small by design


def test_backup_covers_new_table():
    from schwab_cli.backup.core import APPEND_TABLES
    assert "option_chain_snapshots" in APPEND_TABLES
