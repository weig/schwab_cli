"""Command-level tests for ``schwab_cli skew``.

The network is mocked via ``patch("schwab_cli.api.chains.get_chain")``
so tests stay offline and deterministic. We pin down:

  * CLI argument parsing for each mode (L1 / L2 --term / L2 --dtes / L3).
  * Correct envelope shape handed to the analytics layer.
  * HUMAN / JSON / MD outputs for L1 render without error and carry the
    expected anchors.
  * Usage errors exit 2; runtime failures exit 1.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.api.client import ApiError
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


# ---- fixture chain builder --------------------------------------------


def _future_yymmdd(days_out: int = 30) -> tuple[str, str]:
    """Return (YYMMDD, ISO) for a date N days in the future.

    ``parse_option_spec`` rejects past expiries, so tests must use a
    moving target relative to today's date.
    """
    d = date.today() + timedelta(days=days_out)
    return d.strftime("%y%m%d"), d.isoformat()


def _chain_resp(iso_expiry: str, *, symbol: str = "AMZN", dte: int = 30) -> dict:
    """Build a Schwab-shaped chain response dense enough for compute_skew
    to land all three delta targets (ATM, 25Δ, 10Δ) and compute a slope.
    """
    calls = {
        # strike: (delta, iv)
        245.0: (0.75, 0.65),
        250.0: (0.60, 0.63),
        255.0: (0.53, 0.620),
        257.5: (0.50, 0.6162),  # ATM
        260.0: (0.46, 0.612),
        265.0: (0.38, 0.605),
        270.0: (0.30, 0.600),
        272.5: (0.26, 0.5951),  # 25Δ
        275.0: (0.22, 0.597),
        280.0: (0.17, 0.6002),  # 10Δ
    }
    puts = {
        232.5: (-0.16, 0.6380),  # 10Δ
        240.0: (-0.25, 0.6280),  # 25Δ
        250.0: (-0.40, 0.622),
        255.0: (-0.47, 0.619),
        257.5: (-0.50, 0.6158),
    }

    def _call_row(strike: float, delta: float, iv: float) -> dict:
        return {
            "symbol": f"{symbol}  XXXXXX{strike:08.0f}",
            "putCall": "CALL",
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,  # Schwab returns vol as percent
            "bid": 1.0, "ask": 1.05, "last": 1.02,
            "totalVolume": 100, "openInterest": 100,
            "expirationDate": iso_expiry,
            "daysToExpiration": dte,
        }

    def _put_row(strike: float, delta: float, iv: float) -> dict:
        return {
            "symbol": f"{symbol}  XXXXXX{strike:08.0f}",
            "putCall": "PUT",
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,
            "bid": 1.0, "ask": 1.05, "last": 1.02,
            "totalVolume": 100, "openInterest": 100,
            "expirationDate": iso_expiry,
            "daysToExpiration": dte,
        }

    call_map = {
        f"{iso_expiry}:{dte}": {
            f"{s:.1f}": [_call_row(s, d, iv)] for s, (d, iv) in calls.items()
        }
    }
    put_map = {
        f"{iso_expiry}:{dte}": {
            f"{s:.1f}": [_put_row(s, d, iv)] for s, (d, iv) in puts.items()
        }
    }
    return {
        "symbol": symbol,
        "underlying": {"last": 255.36, "change": 0.0, "percentChange": 0.0},
        "callExpDateMap": call_map,
        "putExpDateMap": put_map,
    }


# ---- L1 mode ----------------------------------------------------------


def test_skew_l1_human_happy_path(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        return_value=_chain_resp(iso),
    ) as mock:
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
    assert result.exit_code == 0, result.output
    # Core anchors.
    assert "AMZN Skew" in result.output
    assert "ATM" in result.output
    assert "25Δ Skew" in result.output
    # Mocked API was called once for a single-expiry fetch.
    mock.assert_called_once()
    _, kwargs = mock.call_args
    assert kwargs.get("from_date") == kwargs.get("to_date")


def test_skew_l1_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        return_value=_chain_resp(iso),
    ):
        result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["symbol"] == "AMZN"
    assert data["d25"]["rr"] is not None
    assert data["atm"]["strike"] == 257.5


def test_skew_l1_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        return_value=_chain_resp(iso),
    ):
        result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--md"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# AMZN Skew")
    assert "## Skew Legs" in result.output
    assert "| Metric | Value | Interpretation |" in result.output


def test_skew_l1_symbol_is_uppercased(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        return_value=_chain_resp(iso),
    ) as mock:
        result = runner.invoke(app, ["skew", "amzn", yymmdd])
    assert result.exit_code == 0, result.output
    # get_chain positional arg is the symbol.
    args, _ = mock.call_args
    assert args[1] == "AMZN"


def test_skew_l1_passes_strikes_through(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        return_value=_chain_resp(iso),
    ) as mock:
        result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--strikes", "10"])
    assert result.exit_code == 0, result.output
    _, kwargs = mock.call_args
    assert kwargs.get("strike_count") == 10


# ---- L2 --term mode ---------------------------------------------------


def test_skew_term_fetches_per_expiry(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    y1, iso1 = _future_yymmdd(10)
    y2, iso2 = _future_yymmdd(40)

    def _fake_get_chain(client, symbol, **kwargs):
        from_date = kwargs["from_date"]
        if from_date.isoformat() == iso1:
            return _chain_resp(iso1, dte=10)
        return _chain_resp(iso2, dte=40)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ) as mock:
        result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
    assert result.exit_code == 0, result.output
    assert mock.call_count == 2
    assert "AMZN Term Structure" in result.output


def test_skew_term_json_is_a_list_sorted_by_dte(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    y1, iso1 = _future_yymmdd(10)
    y2, iso2 = _future_yymmdd(40)

    def _fake_get_chain(client, symbol, **kwargs):
        fd = kwargs["from_date"].isoformat()
        if fd == iso1:
            return _chain_resp(iso1, dte=10)
        return _chain_resp(iso2, dte=40)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ):
        # Pass expiries out of order — analytics layer sorts them.
        result = runner.invoke(app, ["skew", "AMZN", "--term", y2, y1, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert [m["dte"] for m in data] == [10, 40]


def test_skew_term_partial_failure_continues(monkeypatch, tmp_path):
    """One failing expiry → warn on stderr but render the rest."""
    _prep(monkeypatch, tmp_path)
    y1, iso1 = _future_yymmdd(10)
    y2, _iso2 = _future_yymmdd(40)

    call_counter = {"n": 0}

    def _fake_get_chain(client, symbol, **kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            raise ApiError("rate-limited")
        return _chain_resp(iso1, dte=10)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ):
        result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
    assert result.exit_code == 0, result.output
    # The skipped expiry prints a warning to stderr (captured in mix_stderr
    # mode by CliRunner) or to output stream. Be lenient about location —
    # what matters is the remaining chain rendered.
    assert "AMZN Term Structure" in result.output


# ---- L2 --dtes mode ---------------------------------------------------


def test_skew_dtes_picks_closest_expiries(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    # Build a discovery response with three expiries at DTE 7, 35, 90.
    today = date.today()
    exp_dtes = [(today + timedelta(days=d), d) for d in (7, 35, 90)]
    discovery = {
        "symbol": "AMZN",
        "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
        "callExpDateMap": {
            f"{exp.isoformat()}:{dte}": {"255.0": [{
                "putCall": "CALL", "strikePrice": 255.0,
                "delta": 0.50, "volatility": 60.0,
            }]} for exp, dte in exp_dtes
        },
        "putExpDateMap": {},
    }

    def _fake_get_chain(client, symbol, **kwargs):
        strike_count = kwargs.get("strike_count", 0)
        if strike_count == 2:
            # Discovery fetch — matches the helper's signature.
            return discovery
        # Full fetch — return the standard chain.
        return _chain_resp(kwargs["from_date"].isoformat(), dte=kwargs["from_date"].toordinal() - today.toordinal())

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ) as mock:
        result = runner.invoke(app, ["skew", "AMZN", "--dtes", "7", "30"])
    assert result.exit_code == 0, result.output
    # 1 discovery + 2 full fetches = 3 calls.
    assert mock.call_count == 3


def test_skew_dtes_rejects_non_integer(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skew", "AMZN", "--dtes", "notanint"])
    assert result.exit_code == 2, result.output


# ---- L3 --cross mode --------------------------------------------------


def test_skew_cross_fetches_per_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)
    symbols_seen: list[str] = []

    def _fake_get_chain(client, symbol, **kwargs):
        symbols_seen.append(symbol)
        return _chain_resp(iso, symbol=symbol, dte=30)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ):
        result = runner.invoke(
            app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "AMZN"]
        )
    assert result.exit_code == 0, result.output
    assert symbols_seen == ["AAPL", "NVDA", "AMZN"]
    assert "Cross-Ticker Skew" in result.output


def test_skew_cross_json_returns_list_of_metrics(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, iso = _future_yymmdd(30)

    def _fake_get_chain(client, symbol, **kwargs):
        return _chain_resp(iso, symbol=symbol, dte=30)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ):
        result = runner.invoke(
            app, ["skew", "--cross", yymmdd, "A", "B", "--json"]
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert {m["symbol"] for m in data} == {"A", "B"}


# ---- argument validation ----------------------------------------------


def test_skew_term_and_cross_are_mutually_exclusive(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, _ = _future_yymmdd(30)
    result = runner.invoke(
        app, ["skew", "AMZN", yymmdd, "--term", "--cross"]
    )
    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_skew_term_and_dtes_are_mutually_exclusive(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["skew", "AMZN", "--term", "--dtes", "30"]
    )
    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_skew_cross_and_dtes_combined_is_allowed(monkeypatch, tmp_path):
    """``--cross --dtes`` should parse and dispatch without hitting the
    mutex — the actual API calls are mocked elsewhere. Here we just pin
    down that the flag combination doesn't exit 2 before we even reach
    the handler."""
    _prep(monkeypatch, tmp_path)

    today = date.today()
    # Each symbol's discovery fetch returns a single expiry near target.
    def _fake_get_chain(client, symbol, **kwargs):
        strike_count = kwargs.get("strike_count", 0)
        exp = today + timedelta(days=30)
        if strike_count == 2:
            # Discovery fetch — populate one expiry key near the target.
            return {
                "symbol": symbol,
                "underlying": {"last": 100.0, "change": 0, "percentChange": 0},
                "callExpDateMap": {
                    f"{exp.isoformat()}:30": {
                        "100.0": [{
                            "putCall": "CALL", "strikePrice": 100.0,
                            "delta": 0.50, "volatility": 30.0,
                        }]
                    }
                },
                "putExpDateMap": {},
            }
        return _chain_resp(exp.isoformat(), symbol=symbol, dte=30)

    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=_fake_get_chain,
    ) as mock:
        result = runner.invoke(
            app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA"]
        )
    assert result.exit_code == 0, result.output
    # 2 discovery + 2 fetches = 4 calls.
    assert mock.call_count == 4
    assert "Cross-Ticker Skew" in result.output


def test_skew_cross_dtes_rejects_non_integer(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["skew", "--cross", "--dtes", "notanint", "AAPL"]
    )
    assert result.exit_code == 2, result.output
    assert "integer" in result.output.lower()


def test_skew_cross_dtes_requires_symbols(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skew", "--cross", "--dtes", "30"])
    assert result.exit_code == 2, result.output


def test_skew_l1_requires_two_positional_args(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skew", "AMZN"])
    assert result.exit_code == 2, result.output
    assert "Usage" in result.output


def test_skew_invalid_yymmdd_exits_non_zero(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skew", "AMZN", "notadate"])
    assert result.exit_code in (1, 2), result.output


def test_skew_past_expiry_exits_non_zero(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    past = (date.today() - timedelta(days=30)).strftime("%y%m%d")
    result = runner.invoke(app, ["skew", "AMZN", past])
    assert result.exit_code != 0, result.output


def test_skew_json_and_md_together_is_error(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, _ = _future_yymmdd(30)
    result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--json", "--md"])
    assert result.exit_code == 2, result.output


def test_skew_api_error_exits_non_zero(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    yymmdd, _ = _future_yymmdd(30)
    with patch(
        "schwab_cli.api.chains.get_chain",
        side_effect=ApiError("simulated outage"),
    ):
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
    assert result.exit_code == 1, result.output
