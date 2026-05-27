"""Unit tests for the Layer-2 ``service.vol.get_vol`` orchestration.

Mocks the Layer-1 chain + history calls (via the module-attribute seams
``api.chains.get_chain`` / ``api.history.get_history``) and uses a real,
tmp-path-isolated vol-history store. Auth is satisfied with a future-dated
session so ``service.auth.get_session`` never fires a token refresh.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotAuthenticated, NotConfigured
from schwab_cli.service.types import VolResult
from schwab_cli.service.vol import get_vol
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

# Near-dated chain (DTE=9), spot 202.50, ATM strike 202.5.
_CHAIN_RESP = {
    "symbol": "NVDA",
    "underlying": {"last": 202.50, "change": 2.62, "percentChange": 1.31},
    "callExpDateMap": {
        "2026-05-01:9": {
            "200.0": [{
                "putCall": "CALL", "strikePrice": 200.0, "volatility": 35.0,
                "totalVolume": 500, "openInterest": 300,
            }],
            "202.5": [{
                "putCall": "CALL", "strikePrice": 202.5, "volatility": 36.58,
                "totalVolume": 1000, "openInterest": 500,
            }],
            "205.0": [{
                "putCall": "CALL", "strikePrice": 205.0, "volatility": 37.5,
                "totalVolume": 200, "openInterest": 150,
            }],
        }
    },
    "putExpDateMap": {
        "2026-05-01:9": {
            "200.0": [{
                "putCall": "PUT", "strikePrice": 200.0, "volatility": 37.0,
                "totalVolume": 300, "openInterest": 200,
            }],
            "202.5": [{
                "putCall": "PUT", "strikePrice": 202.5, "volatility": 36.58,
                "totalVolume": 720, "openInterest": 470,
            }],
            "205.0": [{
                "putCall": "PUT", "strikePrice": 205.0, "volatility": 38.0,
                "totalVolume": 200, "openInterest": 150,
            }],
        }
    },
}


def _history_resp(n_days: int) -> dict:
    return {
        "symbol": "NVDA",
        "candles": [
            {
                "datetime": i * 86_400_000,
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.0 + (1.0 if i % 2 == 0 else -1.0),
                "volume": 1_000_000,
            }
            for i in range(n_days)
        ],
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated HOME + storage with valid config + non-expiring session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))
    return tmp_path


def _store_count(where: str = "") -> int:
    from schwab_cli.storage.vol_history import connect

    clause = f" WHERE {where}" if where else ""
    with connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM vol_snapshots{clause}"
        ).fetchone()[0]


# ---- normal render -----------------------------------------------------


def test_normal_render_returns_volresult(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", return_value=0),
    ):
        result = get_vol("NVDA", no_record=True)

    assert isinstance(result, VolResult)
    env_dict = result.envelope
    assert env_dict["symbol"] == "NVDA"
    assert env_dict["spot"] == 202.5
    assert env_dict["iv"]["strike"] == 202.5
    assert env_dict["iv"]["expiry"] == "2026-05-01"
    assert env_dict["iv"]["dte"] == 9
    assert abs(env_dict["iv"]["value"] - 0.3658) < 1e-3
    assert env_dict["hv"]["window"] == 30
    assert result.storage_error is None


def test_normal_render_flags_propagate(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", return_value=0),
    ):
        result = get_vol(
            "NVDA", hv_window=10, ivp_lookback=100, no_record=True
        )
    assert result.envelope["hv"]["window"] == 10
    assert result.envelope["ivp"]["lookback"] == 100


# ---- snapshot_only -----------------------------------------------------


def test_snapshot_only_returns_none_and_records(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", return_value=0),
    ):
        result = get_vol("NVDA", snapshot_only=True)

    assert result is None
    assert _store_count("source='observed'") >= 1


# ---- no_record ---------------------------------------------------------


def test_no_record_does_not_write(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", return_value=0),
    ):
        result = get_vol("NVDA", no_record=True)

    assert result is not None
    assert _store_count() == 0


def test_no_record_skips_backfill(env):
    """--no-record must not invoke the synthetic backfill at all."""
    calls = {"n": 0}

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return 0

    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", side_effect=_spy),
    ):
        get_vol("NVDA", no_record=True)

    assert calls["n"] == 0


# ---- auth errors -------------------------------------------------------


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    with pytest.raises(NotConfigured):
        get_vol("NVDA", no_record=True)


def test_not_authenticated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    with pytest.raises(NotAuthenticated):
        get_vol("NVDA", no_record=True)


# ---- API error propagation ---------------------------------------------


def test_api_error_on_chain_propagates(env):
    with patch(
        "schwab_cli.api.chains.get_chain", side_effect=ApiError("503 down")
    ):
        with pytest.raises(ApiError):
            get_vol("NVDA", no_record=True)


def test_session_expired_on_history_propagates(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch(
            "schwab_cli.api.history.get_history",
            side_effect=SessionExpired("expired"),
        ),
    ):
        with pytest.raises(SessionExpired):
            get_vol("NVDA", no_record=True)
