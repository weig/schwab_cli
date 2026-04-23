"""Command-level tests for ``schwab_cli vol``.

The command makes two API calls (chain + price history). Both are mocked
here so the tests stay offline and deterministic. We verify the
command's glue — correct parameters to each API, correct envelope
assembly, and the three output formats render without error.
"""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Keep the vol-history DB inside the test's tmp_path so tests can't
    # pollute the user's real `~/.config/schwab_cli/storage/vol_history.db`.
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


# ---- synthetic API responses -------------------------------------------


# Short chain: one near-dated expiry with three strikes. Volume on one
# strike across both legs is ≥ 100 so the ATM picker accepts it.
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
    """
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


# ---- tests --------------------------------------------------------------


def test_vol_happy_path_human(monkeypatch, tmp_path):
    """`vol` in interactive mode renders all rows. We pass --no-record so
    the auto-backfill doesn't fire in tests where we haven't staged
    realistic option-price history (the synthetic side-effect would
    inflate the sample count)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--no-record"])
    assert result.exit_code == 0, result.output

    # Header carries symbol + spot.
    assert "NVDA" in result.output
    assert "$202.50" in result.output
    # Every row label appears.
    for label in ("IV", "HV", "HVP", "P/C vol", "P/C OI", "IVP"):
        assert label in result.output
    # IV value derived from midpoint of call/put at 202.5 strike.
    # Midpoint is 0.3658 → rendered as 36.58%.
    assert "36.58%" in result.output
    # --no-record means IVP has no accumulated samples at all.
    assert "insufficient history" in result.output


def test_vol_json_shape_and_values(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        # --no-record keeps the envelope predictable: no backfill side-effects.
        result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)

    assert env["symbol"] == "NVDA"
    assert env["spot"] == 202.50

    # ATM pick — closest strike to spot with sufficient volume.
    assert env["iv"]["strike"] == 202.5
    assert env["iv"]["expiry"] == "2026-05-01"
    assert env["iv"]["dte"] == 9
    assert abs(env["iv"]["value"] - 0.3658) < 1e-3

    # HV computed from the synthetic oscillating series.
    assert env["hv"]["window"] == 30
    assert env["hv"]["value"] is not None
    assert env["hv"]["value"] > 0

    # HVP has a sample of rolling values. Since the input series is
    # perfectly alternating, the rolling values vary slightly but are
    # all well-defined.
    assert env["hvp"]["value"] is not None
    assert 0 <= env["hvp"]["value"] <= 100

    # P/C ratios: sum(put_vol=300+720+200=1220) / sum(call_vol=500+1000+200=1700) ≈ 0.718
    assert abs(env["pc"]["volume_ratio"] - (1220 / 1700)) < 1e-9
    # OI: 820/950 ≈ 0.863
    assert abs(env["pc"]["oi_ratio"] - (820 / 950)) < 1e-9

    # --no-record means nothing is written. IVP reports insufficient.
    assert env["ivp"]["state"] == "insufficient"
    assert env["ivp"]["value"] is None
    assert env["ivp"]["sample_size"] == 0


def test_vol_md_has_all_rows(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--md"])
    assert result.exit_code == 0, result.output
    for label in ("| IV ", "| HV ", "| HVP ", "| P/C vol ", "| P/C OI ", "| IVP "):
        assert label in result.output


def test_vol_rejects_option_ticker(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["vol", "NVDA260501C240"])
    assert result.exit_code == 2
    assert "stock" in result.output.lower()


def test_vol_missing_spot_exits_1(monkeypatch, tmp_path):
    """If the chain response lacks underlying.last, the command bails cleanly."""
    _prep(monkeypatch, tmp_path)
    resp = {**_CHAIN_RESP, "underlying": {}}
    with patch("schwab_cli.commands.vol.get_chain", return_value=resp), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA"])
    assert result.exit_code == 1
    assert "spot" in result.output.lower()


def test_vol_hv_none_when_history_too_short(monkeypatch, tmp_path):
    """Fewer closes than the HV window means HV and HVP are both None."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(10)):
        result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["hv"]["value"] is None
    assert env["hvp"]["value"] is None
    assert env["hvp"]["sample_size"] == 0


def test_vol_ivp_partial_when_history_between_min_and_lookback(monkeypatch, tmp_path):
    """Seed the store with 60 days of prior snapshots → IVP shows partial."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from schwab_cli.storage.vol_history import connect, record_snapshot

    _prep(monkeypatch, tmp_path)
    NY = ZoneInfo("America/New_York")
    with connect() as conn:
        for i in range(60):
            ts = int(
                datetime(2026, 2, 1, 16, 0, tzinfo=NY).timestamp() * 1000
            ) + i * 86_400_000
            record_snapshot(
                conn, symbol="NVDA", spot=200.0, atm_iv=0.30 + i * 0.001,
                atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
                captured_at_ms=ts,
            )
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--json"])
    env = json.loads(result.output)
    # 60 seeded + 1 from this run = 61 distinct days.
    assert env["ivp"]["state"] == "partial"
    assert env["ivp"]["sample_size"] == 61
    assert env["ivp"]["value"] is not None
    assert 0 <= env["ivp"]["value"] <= 100


def test_vol_ivp_ok_when_history_exceeds_lookback(monkeypatch, tmp_path):
    """With --ivp-lookback=5 and 10 days of history, state is ok."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from schwab_cli.storage.vol_history import connect, record_snapshot

    _prep(monkeypatch, tmp_path)
    NY = ZoneInfo("America/New_York")
    # Seed 30 days with distinct NY dates so the partial sample crosses
    # the 30-day minimum threshold for the "ok" branch.
    with connect() as conn:
        for i in range(30):
            ts = int(
                datetime(2026, 2, 1, 16, 0, tzinfo=NY).timestamp() * 1000
            ) + i * 86_400_000
            record_snapshot(
                conn, symbol="NVDA", spot=200.0, atm_iv=0.30 + i * 0.001,
                atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
                captured_at_ms=ts,
            )
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--ivp-lookback=5", "--json"])
    env = json.loads(result.output)
    assert env["ivp"]["state"] == "ok"
    assert env["ivp"]["lookback"] == 5


def test_vol_no_record_skips_write(monkeypatch, tmp_path):
    """--no-record keeps the store empty after an invocation."""
    from schwab_cli.storage.vol_history import connect

    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--no-record", "--json"])
    assert result.exit_code == 0, result.output
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vol_snapshots").fetchone()[0]
    assert n == 0


def test_vol_snapshot_only_writes_and_is_silent(monkeypatch, tmp_path):
    """--snapshot-only records the snapshot (plus any backfill) without
    rendering. stdout stays empty; stderr may carry the backfill notice
    but we don't require it here."""
    from schwab_cli.storage.vol_history import connect

    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        result = runner.invoke(app, ["vol", "NVDA", "--snapshot-only"])
    assert result.exit_code == 0, result.output
    # stdout stays empty in snapshot-only mode (the backfill notice is
    # suppressed in non-interactive modes).
    assert "IV" not in result.output
    assert "HV" not in result.output
    assert "─" not in result.output  # no rendered header
    # At minimum, today's observed row was written.
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM vol_snapshots WHERE source = 'observed'"
        ).fetchone()[0]
    assert n == 1


def test_vol_backfill_populates_synthetic_rows_on_first_run(monkeypatch, tmp_path):
    """First `vol SYMBOL` run with an empty store triggers the BS backfill.

    The mock option-history is constructed so BS-solving each candle
    yields a valid IV (strike = current spot, sensible premiums). After
    the command runs, the store should contain ≥ 1 observed + many
    synthetic rows, and the envelope should reflect both counts.
    """
    from schwab_cli.storage.vol_history import connect

    _prep(monkeypatch, tmp_path)

    # Timestamps must be recent so T (time to expiry 2026-05-01) is
    # sane — otherwise the BS solver returns absurd IVs that the
    # sanity filter rejects and backfill writes nothing.
    from datetime import datetime, timezone
    base = int(datetime(2026, 4, 1, 20, tzinfo=timezone.utc).timestamp() * 1000)
    ms_per_day = 86_400_000

    # Underlying history: monotonic prices so BS is well-conditioned.
    und = {
        "symbol": "NVDA",
        "candles": [
            {"datetime": base + i * ms_per_day, "open": 200.0, "high": 205.0,
             "low": 198.0, "close": 200.0 + 0.05 * i, "volume": 1_000_000}
            for i in range(20)
        ],
    }
    # Option history: ATM 202.5 call priced near parity + time value.
    # These prices map to sensible IVs in BS.
    opt = {
        "symbol": "NVDA  260501C00202500",
        "candles": [
            {"datetime": base + i * ms_per_day, "open": 4.5, "high": 5.2,
             "low": 4.2, "close": 4.6 + 0.02 * i, "volume": 1000}
            for i in range(20)
        ],
    }

    def fake_history(client, symbol, **kwargs):
        # Differentiate by symbol: the option symbol carries spaces.
        return opt if " " in symbol else und

    with patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", side_effect=fake_history):
        result = runner.invoke(app, ["vol", "NVDA", "--json"])

    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    # Backfill delivered synthetics.
    assert env["ivp"]["synthetic"] >= 1
    assert env["ivp"]["observed"] >= 1
    assert env["ivp"]["sample_size"] == env["ivp"]["synthetic"] + env["ivp"]["observed"]
    # Store carries both sources.
    with connect() as conn:
        src_counts = dict(conn.execute(
            "SELECT source, COUNT(*) FROM vol_snapshots GROUP BY source"
        ).fetchall())
    assert src_counts.get("observed", 0) >= 1
    assert src_counts.get("synthetic", 0) >= 1


def test_vol_backfill_only_fires_on_first_run(monkeypatch, tmp_path):
    """Once a symbol has rows in the store, backfill is skipped."""
    from schwab_cli.storage.vol_history import connect, record_snapshot
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    _prep(monkeypatch, tmp_path)
    NY = ZoneInfo("America/New_York")
    # Pre-seed a few observed days.
    with connect() as conn:
        for i in range(5):
            ts = int(
                datetime(2026, 4, 1, 16, 0, tzinfo=NY).timestamp() * 1000
            ) + i * 86_400_000
            record_snapshot(
                conn, symbol="NVDA", spot=200.0, atm_iv=0.30,
                atm_strike=200.0, atm_expiry="2026-05-01", atm_dte=9,
                captured_at_ms=ts,
            )

    backfill_called = {"count": 0}
    real_backfill = __import__(
        "schwab_cli.commands.vol", fromlist=["_backfill_synthetic_iv"]
    )._backfill_synthetic_iv

    def spy(*args, **kwargs):
        backfill_called["count"] += 1
        return real_backfill(*args, **kwargs)

    with patch("schwab_cli.commands.vol._backfill_synthetic_iv", side_effect=spy), \
         patch("schwab_cli.commands.vol.get_chain", return_value=_CHAIN_RESP), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        runner.invoke(app, ["vol", "NVDA"])

    assert backfill_called["count"] == 0, "backfill should skip when rows exist"


def test_vol_chain_call_uses_wide_params(monkeypatch, tmp_path):
    """Chain call should use ALL contract type and strike_count=60."""
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_chain(client, symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return _CHAIN_RESP

    with patch("schwab_cli.commands.vol.get_chain", side_effect=fake_chain), \
         patch("schwab_cli.commands.vol.get_history", return_value=_history_resp(300)):
        # --no-record suppresses the backfill's extra chain call so the
        # captured dict only reflects the primary vol-window lookup.
        runner.invoke(app, ["vol", "NVDA", "--no-record"])

    assert captured["symbol"] == "NVDA"
    assert captured["contract_type"] == "ALL"
    assert captured["strike_count"] == 60
    assert captured["from_date"] is not None
    assert captured["to_date"] is not None
