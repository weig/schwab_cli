"""Characterization tests for the ``schwab vol`` command.

These tests pin the CURRENT observable behaviour of the vol command
end-to-end so that the upcoming service-layer migration can be proven
behaviour-preserving without altering production code.

## Seams patched by each class (re-point these strings after migration)

| Seam | Current patch target | Post-migration target |
|------|---------------------|----------------------|
| Chain API call | ``schwab_cli.commands.vol.get_chain`` | ``schwab_cli.api.chains.get_chain`` (via service) |
| History API call | ``schwab_cli.commands.vol.get_history`` | ``schwab_cli.api.history.get_history`` (via service) |
| Backfill helper | ``schwab_cli.commands.vol._backfill_synthetic_iv`` | ``schwab_cli.service.vol._backfill_synthetic_iv`` |
| Snapshot write | ``schwab_cli.storage.vol_history.record_snapshot`` | same (storage layer stays) |

## Golden values captured by running current code

All numeric constants in this file were observed by exercising the
production code with the canned ``_CHAIN_RESP`` / ``_history_resp``
fixtures and recording the output verbatim.  Do NOT alter golden
constants without first verifying that the production code changed
intentionally.

## Fixtures reused from test_commands_vol.py

``_CHAIN_RESP`` and ``_history_resp`` are reproduced here verbatim
(not imported) so this file stays self-contained and the constants
can't drift if test_commands_vol.py is refactored.

``_prep`` uses ``expires_at=9_000_000_000`` so the session is always
"fresh" and ``service.auth.get_session`` never fires a real
``oauth.refresh``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _prep(monkeypatch, tmp_path) -> None:
    """Isolated HOME with valid config + non-expiring session.

    ``expires_at=9_000_000_000`` (~year 2255) ensures
    ``service.auth.get_session`` never attempts a real token refresh.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
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
            expires_at=9_000_000_000,
            refresh_token_expires_at=9_000_000_000,
        )
    )


# ---------------------------------------------------------------------------
# Canned API fixtures (reproduced from test_commands_vol.py)
# ---------------------------------------------------------------------------

# Short chain: one near-dated expiry (DTE=9) with three strikes.
# Spot = 202.50; ATM picker will select strike 202.5 (closest to spot,
# highest volume).  P/C aggregates:
#   call_volume = 500+1000+200 = 1700,  put_volume = 300+720+200 = 1220
#   call_oi     = 300+500+150 = 950,    put_oi     = 200+470+150 = 820
#   volume_ratio = 1220/1700 ≈ 0.7176,  oi_ratio = 820/950 ≈ 0.8632
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
    """Deterministic synthetic 1-day candles.

    Prices oscillate ±$1 so log returns are computable and HV is non-zero.
    With n_days=300, the rolling window has enough points for a meaningful
    HVP percentile.
    """
    return {
        "symbol": "NVDA",
        "candles": [
            {
                "datetime": i * 86_400_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + (1.0 if i % 2 == 0 else -1.0),
                "volume": 1_000_000,
            }
            for i in range(n_days)
        ],
    }


# ---------------------------------------------------------------------------
# Golden constants (captured from running current code verbatim)
# ---------------------------------------------------------------------------

# --- JSON golden values (n_days=300, --no-record) ---
_GOLDEN_SYMBOL = "NVDA"
_GOLDEN_SPOT = 202.5
_GOLDEN_IV_STRIKE = 202.5
_GOLDEN_IV_EXPIRY = "2026-05-01"
_GOLDEN_IV_DTE = 9
# IV = midpoint of call/put volatility at 202.5 strike: (36.58+36.58)/2/100 = 0.3658
_GOLDEN_IV_VALUE_APPROX = 0.3658
_GOLDEN_HV_WINDOW_DEFAULT = 30
# HV(30) computed from oscillating series, captured from current code
_GOLDEN_HV_30 = 0.3229284972344688
# HV(10) from same series, different window
_GOLDEN_HV_10 = 0.33467516675002595
# HVP percentile = 50.0 (oscillating series → all rolling HVs identical)
_GOLDEN_HVP_VALUE = 50.0
_GOLDEN_HVP_LOOKBACK_DEFAULT = 252
_GOLDEN_HVP_SAMPLE_252 = 252
# P/C volume ratio: put_volume/call_volume = 1220/1700
_GOLDEN_PC_VOLUME_RATIO = 1220 / 1700
# P/C OI ratio: put_oi/call_oi = 820/950
_GOLDEN_PC_OI_RATIO = 820 / 950
_GOLDEN_PC_CALL_VOLUME = 1700
_GOLDEN_PC_PUT_VOLUME = 1220
_GOLDEN_PC_CALL_OI = 950
_GOLDEN_PC_PUT_OI = 820

# --- JSON top-level key set ---
_GOLDEN_JSON_TOP_KEYS = {"symbol", "spot", "iv", "iv_ref", "hv", "hvp", "pc", "ivp", "ivr_ivp"}
_GOLDEN_JSON_IV_KEYS = {"value", "expiry", "dte", "strike"}
_GOLDEN_JSON_HV_KEYS = {"window", "value"}
_GOLDEN_JSON_HVP_KEYS = {"lookback", "value", "sample_size"}
_GOLDEN_JSON_PC_KEYS = {
    "call_volume", "put_volume", "call_oi", "put_oi",
    "volume_ratio", "oi_ratio",
}
_GOLDEN_JSON_IVP_KEYS = {
    "state", "value", "sample_size", "observed", "synthetic",
    "lookback", "today_iv", "range_min", "range_max",
}

# --- HUMAN golden substrings (--no-record, n_days=300) ---
_GOLDEN_HUMAN_HEADER = "NVDA  $202.50"
_GOLDEN_HUMAN_IV_ROW = "36.58%"
_GOLDEN_HUMAN_IV_NOTE = "ATM 2026-05-01, 9 DTE, strike $202.50"
_GOLDEN_HUMAN_HV_ROW = "32.29%"
_GOLDEN_HUMAN_HV_NOTE = "30-day realized"
_GOLDEN_HUMAN_HVP_ROW = "50%"
_GOLDEN_HUMAN_HVP_NOTE = "252-day percentile"
_GOLDEN_HUMAN_PC_VOL_ROW = "0.72"
_GOLDEN_HUMAN_PC_OI_ROW = "0.86"
_GOLDEN_HUMAN_IVP_INSUFFICIENT = "insufficient history: 0/252 days"
_GOLDEN_HUMAN_LABELS = ("IV", "HV", "HVP", "P/C vol", "P/C OI", "IVP")

# --- MD golden strings (--no-record, n_days=300) ---
_GOLDEN_MD_HEADING = "# NVDA — $202.50"
_GOLDEN_MD_TABLE_HEADER = "| Metric | Value | Context |"
_GOLDEN_MD_TABLE_SEP = "| --- | ---: | --- |"
_GOLDEN_MD_IV_ROW = "| IV | 36.58% | ATM 2026-05-01, 9 DTE, strike $202.50 |"
_GOLDEN_MD_HV_ROW = "| HV | 32.29% | 30-day realized |"
_GOLDEN_MD_HVP_ROW = "| HVP | 50% | 252-day percentile |"
_GOLDEN_MD_PC_VOL_ROW = "| P/C vol | 0.72 | puts/calls, volume, all expiries |"
_GOLDEN_MD_PC_OI_ROW = "| P/C OI | 0.86 | puts/calls, OI, all expiries |"
_GOLDEN_MD_IVP_ROW = "| IVP | — | insufficient history: 0/252 days |"

# --- Short-history (n_days=10) golden ---
_GOLDEN_SHORT_HV_VALUE = None
_GOLDEN_SHORT_HVP_VALUE = None
_GOLDEN_SHORT_HVP_SAMPLE = 0
_GOLDEN_SHORT_MD_HV_ROW = "| HV | — | 30-day realized |"
_GOLDEN_SHORT_MD_HVP_ROW = "| HVP | — | 252-day percentile (0/252 available) |"
_GOLDEN_SHORT_MD_IVP_ROW = "| IVP | — | insufficient history: 0/252 days |"


# ===========================================================================
# 1. Golden HUMAN output — normal vol NVDA --no-record
# ===========================================================================


class TestGoldenHumanOutput:
    """Pin HUMAN output for normal vol NVDA with canned chain+history.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
    """

    def _invoke(self, monkeypatch, tmp_path) -> str:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_exit_0(self, monkeypatch, tmp_path):
        """Happy-path must exit 0."""
        out = self._invoke(monkeypatch, tmp_path)
        assert out  # non-empty

    def test_header_contains_symbol_and_spot(self, monkeypatch, tmp_path):
        """Header line must contain symbol and formatted spot price."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_HEADER in out

    def test_all_row_labels_present(self, monkeypatch, tmp_path):
        """All metric row labels must appear in output."""
        out = self._invoke(monkeypatch, tmp_path)
        for label in _GOLDEN_HUMAN_LABELS:
            assert label in out, f"Missing label: {label!r}"

    def test_iv_value_formatted(self, monkeypatch, tmp_path):
        """IV must be rendered as '36.58%' (midpoint of call+put at 202.5)."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_IV_ROW in out

    def test_iv_note_contains_expiry_dte_strike(self, monkeypatch, tmp_path):
        """IV note must show expiry, DTE, and strike."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_IV_NOTE in out

    def test_hv_value_formatted(self, monkeypatch, tmp_path):
        """HV must be rendered as '32.29%' for the oscillating 300-day series."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_HV_ROW in out

    def test_hv_note_contains_window(self, monkeypatch, tmp_path):
        """HV note must say '30-day realized'."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_HV_NOTE in out

    def test_hvp_value_formatted(self, monkeypatch, tmp_path):
        """HVP must be rendered as '50%' (median of identical rolling HVs)."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_HVP_ROW in out

    def test_hvp_note_contains_lookback(self, monkeypatch, tmp_path):
        """HVP note must say '252-day percentile'."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_HVP_NOTE in out

    def test_pc_volume_ratio_formatted(self, monkeypatch, tmp_path):
        """P/C vol ratio must be rendered as '0.72' (1220/1700 rounded)."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_PC_VOL_ROW in out

    def test_pc_oi_ratio_formatted(self, monkeypatch, tmp_path):
        """P/C OI ratio must be rendered as '0.86' (820/950 rounded)."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_PC_OI_ROW in out

    def test_ivp_shows_insufficient_with_no_record(self, monkeypatch, tmp_path):
        """--no-record means no accumulated samples → IVP renders as
        'insufficient history: 0/252 days'."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_HUMAN_IVP_INSUFFICIENT in out


# ===========================================================================
# 2. Golden JSON output — exact keys and values
# ===========================================================================


class TestGoldenJsonOutput:
    """Pin JSON envelope structure and pinned values.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
    """

    def _env(self, monkeypatch, tmp_path) -> dict:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def test_exit_0(self, monkeypatch, tmp_path):
        env = self._env(monkeypatch, tmp_path)
        assert env  # non-empty dict

    def test_top_level_keys(self, monkeypatch, tmp_path):
        """JSON envelope must have exactly the pinned top-level key set."""
        env = self._env(monkeypatch, tmp_path)
        assert set(env.keys()) == _GOLDEN_JSON_TOP_KEYS

    def test_symbol_and_spot(self, monkeypatch, tmp_path):
        """symbol and spot must be NVDA / 202.5."""
        env = self._env(monkeypatch, tmp_path)
        assert env["symbol"] == _GOLDEN_SYMBOL
        assert env["spot"] == _GOLDEN_SPOT

    def test_iv_sub_keys(self, monkeypatch, tmp_path):
        """iv sub-object must have exactly the pinned keys."""
        env = self._env(monkeypatch, tmp_path)
        assert set(env["iv"].keys()) == _GOLDEN_JSON_IV_KEYS

    def test_iv_strike_expiry_dte(self, monkeypatch, tmp_path):
        """iv must carry the ATM-picked strike, expiry, and DTE from chain."""
        env = self._env(monkeypatch, tmp_path)
        assert env["iv"]["strike"] == _GOLDEN_IV_STRIKE
        assert env["iv"]["expiry"] == _GOLDEN_IV_EXPIRY
        assert env["iv"]["dte"] == _GOLDEN_IV_DTE

    def test_iv_value_approx(self, monkeypatch, tmp_path):
        """iv.value must be near 0.3658 (midpoint volatility at 202.5 strike)."""
        env = self._env(monkeypatch, tmp_path)
        assert abs(env["iv"]["value"] - _GOLDEN_IV_VALUE_APPROX) < 1e-3

    def test_iv_ref_is_null(self, monkeypatch, tmp_path):
        """iv_ref must be null when no long-dated expiry exists in chain."""
        env = self._env(monkeypatch, tmp_path)
        assert env["iv_ref"] is None

    def test_hv_sub_keys(self, monkeypatch, tmp_path):
        env = self._env(monkeypatch, tmp_path)
        assert set(env["hv"].keys()) == _GOLDEN_JSON_HV_KEYS

    def test_hv_window_and_value(self, monkeypatch, tmp_path):
        """hv.window must equal 30 (default); hv.value must be exact golden."""
        env = self._env(monkeypatch, tmp_path)
        assert env["hv"]["window"] == _GOLDEN_HV_WINDOW_DEFAULT
        assert abs(env["hv"]["value"] - _GOLDEN_HV_30) < 1e-10

    def test_hvp_sub_keys(self, monkeypatch, tmp_path):
        env = self._env(monkeypatch, tmp_path)
        assert set(env["hvp"].keys()) == _GOLDEN_JSON_HVP_KEYS

    def test_hvp_value_and_lookback_and_sample(self, monkeypatch, tmp_path):
        """hvp.value=50.0, lookback=252, sample_size=252 for the 300-day series."""
        env = self._env(monkeypatch, tmp_path)
        assert env["hvp"]["value"] == _GOLDEN_HVP_VALUE
        assert env["hvp"]["lookback"] == _GOLDEN_HVP_LOOKBACK_DEFAULT
        assert env["hvp"]["sample_size"] == _GOLDEN_HVP_SAMPLE_252

    def test_pc_sub_keys(self, monkeypatch, tmp_path):
        env = self._env(monkeypatch, tmp_path)
        assert set(env["pc"].keys()) == _GOLDEN_JSON_PC_KEYS

    def test_pc_raw_volumes_and_oi(self, monkeypatch, tmp_path):
        """Raw put/call volume and OI sums must match aggregated canned values."""
        env = self._env(monkeypatch, tmp_path)
        assert env["pc"]["call_volume"] == _GOLDEN_PC_CALL_VOLUME
        assert env["pc"]["put_volume"] == _GOLDEN_PC_PUT_VOLUME
        assert env["pc"]["call_oi"] == _GOLDEN_PC_CALL_OI
        assert env["pc"]["put_oi"] == _GOLDEN_PC_PUT_OI

    def test_pc_volume_ratio(self, monkeypatch, tmp_path):
        """volume_ratio = put/call = 1220/1700 (exact float equality)."""
        env = self._env(monkeypatch, tmp_path)
        assert abs(env["pc"]["volume_ratio"] - _GOLDEN_PC_VOLUME_RATIO) < 1e-9

    def test_pc_oi_ratio(self, monkeypatch, tmp_path):
        """oi_ratio = put/call = 820/950 (exact float equality)."""
        env = self._env(monkeypatch, tmp_path)
        assert abs(env["pc"]["oi_ratio"] - _GOLDEN_PC_OI_RATIO) < 1e-9

    def test_ivp_sub_keys(self, monkeypatch, tmp_path):
        env = self._env(monkeypatch, tmp_path)
        assert set(env["ivp"].keys()) == _GOLDEN_JSON_IVP_KEYS

    def test_ivp_state_insufficient_with_no_record(self, monkeypatch, tmp_path):
        """--no-record → no store writes → ivp.state == 'insufficient'."""
        env = self._env(monkeypatch, tmp_path)
        assert env["ivp"]["state"] == "insufficient"
        assert env["ivp"]["value"] is None
        assert env["ivp"]["sample_size"] == 0

    def test_ivr_ivp_block_insufficient_with_no_record(self, monkeypatch, tmp_path):
        """ivr_ivp block must report null ivr/ivp and low_history when no store data."""
        env = self._env(monkeypatch, tmp_path)
        assert env["ivr_ivp"]["ivr"] is None
        assert env["ivr_ivp"]["ivp"] is None
        assert env["ivr_ivp"]["n_days"] == 0
        assert env["ivr_ivp"]["source"] == "insufficient"
        assert env["ivr_ivp"].get("low_history") is True


# ===========================================================================
# 3. Golden MD output — exact header, table structure, and row values
# ===========================================================================


class TestGoldenMdOutput:
    """Pin MD format output for normal vol NVDA.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
    """

    def _invoke(self, monkeypatch, tmp_path) -> str:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--md"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_exit_0(self, monkeypatch, tmp_path):
        out = self._invoke(monkeypatch, tmp_path)
        assert out

    def test_h1_heading(self, monkeypatch, tmp_path):
        """MD output must start with the exact H1 heading."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_HEADING in out
        # heading must be first content line
        first_line = out.splitlines()[0]
        assert first_line.startswith("# ")

    def test_table_header_and_separator(self, monkeypatch, tmp_path):
        """MD table must have exact header and right-aligned separator rows."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_TABLE_HEADER in out
        assert _GOLDEN_MD_TABLE_SEP in out

    def test_iv_row_exact(self, monkeypatch, tmp_path):
        """IV row must exactly match golden MD string."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_IV_ROW in out

    def test_hv_row_exact(self, monkeypatch, tmp_path):
        """HV row must exactly match golden MD string."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_HV_ROW in out

    def test_hvp_row_exact(self, monkeypatch, tmp_path):
        """HVP row must exactly match golden MD string."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_HVP_ROW in out

    def test_pc_vol_row_exact(self, monkeypatch, tmp_path):
        """P/C vol row must exactly match golden MD string."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_PC_VOL_ROW in out

    def test_pc_oi_row_exact(self, monkeypatch, tmp_path):
        """P/C OI row must exactly match golden MD string."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_PC_OI_ROW in out

    def test_ivp_row_exact(self, monkeypatch, tmp_path):
        """IVP row must render '—' and 'insufficient history' note."""
        out = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_IVP_ROW in out

    def test_is_valid_markdown(self, monkeypatch, tmp_path):
        """MD output must start with '#' and contain at least one pipe table row."""
        out = self._invoke(monkeypatch, tmp_path)
        lines = out.splitlines()
        assert lines[0].startswith("# ")
        assert any("|" in ln for ln in lines)


# ===========================================================================
# 4. --hv-window and --hv-lookback change computed values
# ===========================================================================


class TestHvWindowAndLookback:
    """Pin that flag values propagate through to the HV/HVP envelope.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
    """

    def _env(self, monkeypatch, tmp_path, extra_flags: list[str]) -> dict:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(
                app, ["vol", "NVDA", "--no-record", "--json"] + extra_flags
            )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def test_hv_window_10_differs_from_default_30(self, monkeypatch, tmp_path):
        """--hv-window=10 must produce a different HV value than the default 30."""
        env10 = self._env(monkeypatch, tmp_path, ["--hv-window=10"])
        env30 = self._env(monkeypatch, tmp_path, [])
        assert env10["hv"]["window"] == 10
        assert env30["hv"]["window"] == 30
        assert env10["hv"]["value"] != env30["hv"]["value"]

    def test_hv_window_10_exact_golden(self, monkeypatch, tmp_path):
        """HV with window=10 must equal the exact golden value captured from code."""
        env = self._env(monkeypatch, tmp_path, ["--hv-window=10"])
        assert abs(env["hv"]["value"] - _GOLDEN_HV_10) < 1e-10

    def test_hv_window_30_exact_golden(self, monkeypatch, tmp_path):
        """HV with default window=30 must equal the exact golden value."""
        env = self._env(monkeypatch, tmp_path, [])
        assert abs(env["hv"]["value"] - _GOLDEN_HV_30) < 1e-10

    def test_hv_lookback_50_clips_sample(self, monkeypatch, tmp_path):
        """--hv-lookback=50 must clip the HVP series to at most 50 samples."""
        env = self._env(monkeypatch, tmp_path, ["--hv-lookback=50"])
        assert env["hvp"]["lookback"] == 50
        assert env["hvp"]["sample_size"] == 50

    def test_hv_lookback_50_hvp_value_golden(self, monkeypatch, tmp_path):
        """HVP with --hv-lookback=50 must equal 50.0 (same oscillating series)."""
        env = self._env(monkeypatch, tmp_path, ["--hv-lookback=50"])
        assert env["hvp"]["value"] == 50.0

    def test_hv_window_propagates_into_envelope(self, monkeypatch, tmp_path):
        """hv.window in the envelope must reflect the --hv-window flag."""
        for window in (10, 20, 30):
            env = self._env(monkeypatch, tmp_path, [f"--hv-window={window}"])
            assert env["hv"]["window"] == window

    def test_ivp_lookback_propagates_into_envelope(self, monkeypatch, tmp_path):
        """ivp.lookback must reflect the --ivp-lookback flag."""
        env = self._env(monkeypatch, tmp_path, ["--ivp-lookback=100"])
        assert env["ivp"]["lookback"] == 100


# ===========================================================================
# 5. --snapshot-only: writes silently, no rendered output
# ===========================================================================


class TestSnapshotOnly:
    """Pin --snapshot-only behaviour: records and exits with empty stdout.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
           schwab_cli.storage.vol_history.record_snapshot (write spy)
    """

    def test_exit_0(self, monkeypatch, tmp_path):
        """--snapshot-only must exit 0 on success."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--snapshot-only"])
        assert result.exit_code == 0

    def test_stdout_is_empty(self, monkeypatch, tmp_path):
        """--snapshot-only must produce no output on stdout."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--snapshot-only"])
        assert result.output.strip() == ""

    def test_no_iv_hv_rows_in_stdout(self, monkeypatch, tmp_path):
        """Rendered metric rows (IV, HV, separator) must not appear in stdout."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--snapshot-only"])
        assert "IV" not in result.output
        assert "HV" not in result.output
        assert "─" not in result.output

    def test_writes_observed_row_to_store(self, monkeypatch, tmp_path):
        """--snapshot-only must write at least one observed row to the store."""
        from schwab_cli.storage.vol_history import connect

        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--snapshot-only"])
        assert result.exit_code == 0
        with connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM vol_snapshots WHERE source='observed'"
            ).fetchone()[0]
        assert n >= 1


# ===========================================================================
# 6. --no-record: renders but does NOT write to store
# ===========================================================================


class TestNoRecord:
    """Pin --no-record: full render is produced but store write is skipped.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized),
           schwab_cli.storage.vol_history.record_snapshot (spy)
    """

    def test_exit_0(self, monkeypatch, tmp_path):
        """--no-record must still exit 0."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
        assert result.exit_code == 0

    def test_record_snapshot_not_called(self, monkeypatch, tmp_path):
        """record_snapshot must NOT be called when --no-record is passed.

        Spy seam: schwab_cli.storage.vol_history.record_snapshot
        """
        _prep(monkeypatch, tmp_path)
        record_calls: list = []
        from schwab_cli.storage import vol_history as _vh

        orig = _vh.record_snapshot

        def _spy(*args, **kwargs):
            record_calls.append((args, kwargs))
            return orig(*args, **kwargs)

        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
            patch("schwab_cli.storage.vol_history.record_snapshot", side_effect=_spy),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
        assert result.exit_code == 0
        assert len(record_calls) == 0, (
            f"record_snapshot must not be called with --no-record, "
            f"got {len(record_calls)} call(s)"
        )

    def test_store_stays_empty(self, monkeypatch, tmp_path):
        """After --no-record, the store must contain zero rows."""
        from schwab_cli.storage.vol_history import connect

        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
        with connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM vol_snapshots").fetchone()[0]
        assert n == 0

    def test_full_render_is_produced(self, monkeypatch, tmp_path):
        """--no-record must still produce a full render (human format)."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "IV" in result.output
        assert "HV" in result.output
        assert "NVDA" in result.output


# ===========================================================================
# 7. Short history / insufficient data
# ===========================================================================


class TestShortHistory:
    """Pin rendered output when history is too short to compute HV/HVP.

    With _history_resp(10) the underlying has only 10 candles — fewer than
    the default 30-day HV window — so HV and HVP are both null/dash.

    Seams: schwab_cli.commands.vol.get_chain, schwab_cli.commands.vol.get_history,
           schwab_cli.commands.vol._backfill_synthetic_iv (neutralized)
    """

    def _env(self, monkeypatch, tmp_path) -> dict:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(10)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    def _md(self, monkeypatch, tmp_path) -> str:
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(10)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--md"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_hv_value_is_null(self, monkeypatch, tmp_path):
        """hv.value must be null when fewer candles than the window."""
        env = self._env(monkeypatch, tmp_path)
        assert env["hv"]["value"] is _GOLDEN_SHORT_HV_VALUE

    def test_hvp_value_is_null(self, monkeypatch, tmp_path):
        """hvp.value must be null when hv_series is empty."""
        env = self._env(monkeypatch, tmp_path)
        assert env["hvp"]["value"] is _GOLDEN_SHORT_HVP_VALUE

    def test_hvp_sample_size_zero(self, monkeypatch, tmp_path):
        """hvp.sample_size must be 0 when no rolling HV values can be computed."""
        env = self._env(monkeypatch, tmp_path)
        assert env["hvp"]["sample_size"] == _GOLDEN_SHORT_HVP_SAMPLE

    def test_iv_still_rendered(self, monkeypatch, tmp_path):
        """IV must still appear (chain is independent of history length)."""
        env = self._env(monkeypatch, tmp_path)
        assert env["iv"]["value"] is not None
        assert abs(env["iv"]["value"] - _GOLDEN_IV_VALUE_APPROX) < 1e-3

    def test_md_hv_row_shows_dash(self, monkeypatch, tmp_path):
        """MD HV row must show '—' when history is too short."""
        out = self._md(monkeypatch, tmp_path)
        assert _GOLDEN_SHORT_MD_HV_ROW in out

    def test_md_hvp_row_shows_dash_and_availability(self, monkeypatch, tmp_path):
        """MD HVP row must show '—' and '0/252 available' note."""
        out = self._md(monkeypatch, tmp_path)
        assert _GOLDEN_SHORT_MD_HVP_ROW in out

    def test_md_ivp_row_shows_insufficient(self, monkeypatch, tmp_path):
        """MD IVP row must show '—' and insufficient-history note."""
        out = self._md(monkeypatch, tmp_path)
        assert _GOLDEN_SHORT_MD_IVP_ROW in out

    def test_exit_code_0(self, monkeypatch, tmp_path):
        """Short history must still exit 0 (graceful degradation)."""
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(10)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 0


# ===========================================================================
# 8. Error / exit-code paths
# ===========================================================================


class TestErrorPaths:
    """Pin all error exit codes and message substrings.

    Seams vary by test case — each method documents its own patch targets.
    """

    # --- Flag-validation errors (exit 2, before any API call) ---

    def test_both_json_and_md_flags_exit_2(self, monkeypatch, tmp_path):
        """--json and --md together must exit 2 (mutually exclusive).

        Seam: none — error is raised before API calls.
        """
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA", "--json", "--md"])
        assert result.exit_code == 2

    def test_both_json_and_md_flags_message(self, monkeypatch, tmp_path):
        """Mutual-exclusion error must say 'mutually exclusive'."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA", "--json", "--md"])
        assert "mutually exclusive" in result.output

    # --- Ticker parse errors (exit 2) ---

    def test_option_ticker_exit_2(self, monkeypatch, tmp_path):
        """An option ticker passed to vol must exit 2.

        Seam: none — resolved before API calls.
        """
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA260501C202.5"])
        assert result.exit_code == 2

    def test_option_ticker_message(self, monkeypatch, tmp_path):
        """Option ticker error must mention 'stock' and 'option'."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA260501C202.5"])
        assert "stock" in result.output.lower()

    def test_unrecognized_ticker_exit_2(self, monkeypatch, tmp_path):
        """An unrecognized ticker format must exit 2.

        Seam: none — TickerError raised before API calls.
        """
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA!!!"])
        assert result.exit_code == 2

    def test_unrecognized_ticker_message(self, monkeypatch, tmp_path):
        """Unrecognized ticker error must say 'unrecognized ticker format'."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["vol", "NVDA!!!"])
        assert "unrecognized ticker" in result.output.lower()

    # --- Config / session absent (exit 1) ---

    def test_no_config_exit_1(self, monkeypatch, tmp_path):
        """Missing config must exit 1.

        Seam: none — _client() raises before any API call.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = runner.invoke(app, ["vol", "NVDA"])
        assert result.exit_code == 1

    def test_no_config_message(self, monkeypatch, tmp_path):
        """Missing config must print 'No config found'."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = runner.invoke(app, ["vol", "NVDA"])
        assert "No config" in result.output

    def test_no_session_exit_1(self, monkeypatch, tmp_path):
        """Config present but no session must exit 1.

        Seam: none — _client() raises before any API call.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(app, ["vol", "NVDA"])
        assert result.exit_code == 1

    def test_no_session_message(self, monkeypatch, tmp_path):
        """Missing session must print 'No session found'."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(app, ["vol", "NVDA"])
        assert "No session" in result.output

    # --- API call failures (exit 1) ---

    def test_api_error_on_chain_exit_1(self, monkeypatch, tmp_path):
        """ApiError from get_chain must exit 1.

        Seam: schwab_cli.commands.vol.get_chain
        """
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.vol.get_chain",
            side_effect=ApiError("503 down"),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 1

    def test_api_error_on_chain_message(self, monkeypatch, tmp_path):
        """ApiError message must appear in output."""
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.vol.get_chain",
            side_effect=ApiError("503 down"),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "503 down" in result.output

    def test_session_expired_on_chain_exit_1(self, monkeypatch, tmp_path):
        """SessionExpired from get_chain must exit 1.

        Seam: schwab_cli.commands.vol.get_chain
        """
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.vol.get_chain",
            side_effect=SessionExpired("token expired"),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 1

    def test_session_expired_on_chain_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            "schwab_cli.commands.vol.get_chain",
            side_effect=SessionExpired("token expired"),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "token expired" in result.output

    def test_api_error_on_history_exit_1(self, monkeypatch, tmp_path):
        """ApiError from get_history must exit 1.

        Seams: schwab_cli.commands.vol.get_chain (passes),
               schwab_cli.commands.vol.get_history (raises ApiError)
        """
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch(
                "schwab_cli.commands.vol.get_history",
                side_effect=ApiError("rate limited"),
            ),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 1

    def test_api_error_on_history_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch(
                "schwab_cli.commands.vol.get_history",
                side_effect=ApiError("rate limited"),
            ),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "rate limited" in result.output

    def test_session_expired_on_history_exit_1(self, monkeypatch, tmp_path):
        """SessionExpired from get_history must exit 1.

        Seams: schwab_cli.commands.vol.get_chain (passes),
               schwab_cli.commands.vol.get_history (raises SessionExpired)
        """
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch(
                "schwab_cli.commands.vol.get_history",
                side_effect=SessionExpired("expired"),
            ),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 1

    def test_session_expired_on_history_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with (
            patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP),
            patch(
                "schwab_cli.commands.vol.get_history",
                side_effect=SessionExpired("expired"),
            ),
        ):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "expired" in result.output

    def test_missing_spot_in_chain_exit_1(self, monkeypatch, tmp_path):
        """Chain response with no underlying.last must exit 1.

        Seam: schwab_cli.commands.vol.get_chain
        """
        _prep(monkeypatch, tmp_path)
        resp = {**_CHAIN_RESP, "underlying": {}}
        with patch("schwab_cli.commands.vol.get_chain", return_value=resp):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert result.exit_code == 1

    def test_missing_spot_message(self, monkeypatch, tmp_path):
        """Missing spot error must mention 'spot'."""
        _prep(monkeypatch, tmp_path)
        resp = {**_CHAIN_RESP, "underlying": {}}
        with patch("schwab_cli.commands.vol.get_chain", return_value=resp):
            result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
        assert "spot" in result.output.lower()


# ===========================================================================
# 9. Chain call parameters (structural invariant)
# ===========================================================================


class TestChainCallParameters:
    """Pin that get_chain is called with contract_type=ALL and strike_count=60.

    Seam: schwab_cli.commands.vol.get_chain (capture kwargs)
    """

    def test_chain_called_with_wide_params(self, monkeypatch, tmp_path):
        """Chain call must use ALL contracts and strike_count=60."""
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def _fake_chain(client, symbol, **kwargs):
            captured.update(kwargs)
            captured["symbol"] = symbol
            return _CHAIN_RESP

        with (
            patch("schwab_cli.commands.vol.get_chain", side_effect=_fake_chain),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            runner.invoke(app, ["vol", "NVDA", "--no-record"])

        assert captured["symbol"] == "NVDA"
        assert captured["contract_type"] == "ALL"
        assert captured["strike_count"] == 60
        assert captured["from_date"] is not None
        assert captured["to_date"] is not None

    def test_chain_to_date_is_in_future(self, monkeypatch, tmp_path):
        """to_date must be > from_date (1.5-year expiry window)."""
        from datetime import date

        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def _fake_chain(client, symbol, **kwargs):
            captured.update(kwargs)
            return _CHAIN_RESP

        with (
            patch("schwab_cli.commands.vol.get_chain", side_effect=_fake_chain),
            patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)),
            patch("schwab_cli.commands.vol._backfill_synthetic_iv", return_value=0),
        ):
            runner.invoke(app, ["vol", "NVDA", "--no-record"])

        assert captured["to_date"] > captured["from_date"]
        # to_date should be >300 days out (540 days in the command)
        diff = (captured["to_date"] - date.today()).days
        assert diff > 300
