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
from schwab_cli.service.vol import VolService
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
        result = VolService().get_vol("NVDA", no_record=True)

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
        result = VolService().get_vol(
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
        result = VolService().get_vol("NVDA", snapshot_only=True)

    assert result is None
    assert _store_count("source='observed'") >= 1


# ---- no_record ---------------------------------------------------------


def test_no_record_does_not_write(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch("schwab_cli.api.history.get_history", return_value=_history_resp(300)),
        patch("schwab_cli.service.vol._backfill_synthetic_iv", return_value=0),
    ):
        result = VolService().get_vol("NVDA", no_record=True)

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
        VolService().get_vol("NVDA", no_record=True)

    assert calls["n"] == 0


# ---- auth errors -------------------------------------------------------


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    with pytest.raises(NotConfigured):
        VolService().get_vol("NVDA", no_record=True)


def test_not_authenticated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    with pytest.raises(NotAuthenticated):
        VolService().get_vol("NVDA", no_record=True)


# ---- API error propagation ---------------------------------------------


def test_api_error_on_chain_propagates(env):
    with patch(
        "schwab_cli.api.chains.get_chain", side_effect=ApiError("503 down")
    ):
        with pytest.raises(ApiError):
            VolService().get_vol("NVDA", no_record=True)


def test_session_expired_on_history_propagates(env):
    with (
        patch("schwab_cli.api.chains.get_chain", return_value=_CHAIN_RESP),
        patch(
            "schwab_cli.api.history.get_history",
            side_effect=SessionExpired("expired"),
        ),
    ):
        with pytest.raises(SessionExpired):
            VolService().get_vol("NVDA", no_record=True)


# ---- _compute_ivp_state: defense-in-depth against non-positive IVs ----
#
# Even if the read path is already filtered, _compute_ivp_state must
# defensively discard any series entry with iv <= 0 before computing
# range_min / range_max / sample_size / observed / synthetic.
#
# This ensures a stale bad row that reaches this layer (e.g. from a
# synthetic backfill or a future code path) cannot corrupt the output.


def test_compute_ivp_state_excludes_sentinel_from_range_and_count():
    """_compute_ivp_state must drop (-9.99, 'observed') and only use
    positive iv values for range_min, range_max, and sample_size."""
    from schwab_cli.service.vol import _compute_ivp_state
    from schwab_cli.storage.vol_history import SOURCE_OBSERVED, SOURCE_SYNTHETIC

    # Mix: one stored-sentinel value and three valid observed values.
    # The sentinel is the minimum numerically — range_min must NOT be -9.99.
    series_tagged = [
        (-9.99, SOURCE_OBSERVED),   # stored form of the -999.0 sentinel
        (0.25,  SOURCE_OBSERVED),
        (0.30,  SOURCE_OBSERVED),
        (0.35,  SOURCE_SYNTHETIC),
    ]
    result = _compute_ivp_state(
        series_tagged=series_tagged,
        today_iv=0.28,
        lookback=252,
    )

    # range_min must be the smallest POSITIVE value (0.25), not -9.99.
    assert result["range_min"] == pytest.approx(0.25)
    # range_max must reflect valid values only.
    assert result["range_max"] == pytest.approx(0.35)
    # The bad row must not count toward sample_size.
    assert result["sample_size"] == 3
    # Observed count: only the 2 good observed rows (0.25, 0.30).
    assert result["observed"] == 2
    # Synthetic count: 1 (0.35).
    assert result["synthetic"] == 1


def test_compute_ivp_state_excludes_zero_iv_from_range():
    """Zero IV is also non-positive and must be dropped."""
    from schwab_cli.service.vol import _compute_ivp_state
    from schwab_cli.storage.vol_history import SOURCE_OBSERVED

    series_tagged = [
        (0.0,  SOURCE_OBSERVED),   # zero IV — must be excluded
        (0.20, SOURCE_OBSERVED),
        (0.22, SOURCE_OBSERVED),
    ]
    result = _compute_ivp_state(
        series_tagged=series_tagged,
        today_iv=0.21,
        lookback=252,
    )

    assert result["range_min"] == pytest.approx(0.20)
    assert result["range_max"] == pytest.approx(0.22)
    assert result["sample_size"] == 2
    assert result["observed"] == 2


def test_compute_ivp_state_all_valid_entries_unchanged():
    """When all series entries are positive, behavior is unchanged."""
    from schwab_cli.service.vol import _compute_ivp_state
    from schwab_cli.storage.vol_history import SOURCE_OBSERVED

    series_tagged = [
        (0.20, SOURCE_OBSERVED),
        (0.25, SOURCE_OBSERVED),
        (0.30, SOURCE_OBSERVED),
    ]
    result = _compute_ivp_state(
        series_tagged=series_tagged,
        today_iv=0.22,
        lookback=252,
    )

    assert result["range_min"] == pytest.approx(0.20)
    assert result["range_max"] == pytest.approx(0.30)
    assert result["sample_size"] == 3


def test_compute_ivp_state_only_sentinel_entries_yields_insufficient():
    """If every entry is non-positive, the result must be 'insufficient'
    with None range_min / range_max and sample_size == 0."""
    from schwab_cli.service.vol import _compute_ivp_state
    from schwab_cli.storage.vol_history import SOURCE_OBSERVED

    series_tagged = [
        (-9.99, SOURCE_OBSERVED),
        (0.0,   SOURCE_OBSERVED),
        (-5.0,  SOURCE_OBSERVED),
    ]
    result = _compute_ivp_state(
        series_tagged=series_tagged,
        today_iv=0.28,
        lookback=252,
    )

    assert result["state"] == "insufficient"
    assert result["value"] is None
    assert result["sample_size"] == 0
    assert result["range_min"] is None
    assert result["range_max"] is None
