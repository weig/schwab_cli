import time
from datetime import date
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.service.auth import ApiError, SessionExpired
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
    now = int(time.time())
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=now + 3600,
        refresh_token_expires_at=now + 7 * 24 * 3600,
    ))


_DIV = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14},
        "fundamental": {
            "dividendAmount": 1.0,
            "dividendYield": 0.44,
            "dividendFreq": 4,
            "dividendDate": "2025-05-12 04:00:00.0",
            "dividendPayAmount": 0.25,
            "dividendPayDate": "2025-05-15 04:00:00.0",
            "nextDividendDate": "2025-08-12 04:00:00.0",
            "nextDividendPayDate": "2025-08-15 04:00:00.0",
        },
    },
    "KO": {
        "symbol": "KO",
        "quote": {"lastPrice": 70.0},
        "fundamental": {
            "dividendAmount": 2.04,
            "dividendYield": 2.91,
            "dividendFreq": 4,
            "nextDividendDate": "2025-09-15 04:00:00.0",
            "nextDividendPayDate": "2025-10-01 04:00:00.0",
        },
    },
}


def test_dividends_happy(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        return_value=_DIV,
    ) as mock:
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "0.25" in result.output
    # Calls the endpoint with fundamental fields.
    _, kwargs = mock.call_args
    assert kwargs.get("fields") == "all"


def test_dividends_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        return_value=_DIV,
    ):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    import json
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["next_ex_date"].startswith("2025-08-12")


def test_dividends_upcoming_window_filters(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    # Freeze date in the output module so delta-days are deterministic.
    from schwab_cli.output import dividends as div_out
    monkeypatch.setattr(div_out, "_today", lambda: date(2025, 7, 15))
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        return_value=_DIV,
    ):
        result = runner.invoke(
            app, ["dividends", "AAPL", "KO", "--upcoming", "--within-days", "30"]
        )
    assert result.exit_code == 0
    assert "AAPL" in result.output
    assert "KO" not in result.output


def test_dividends_upcoming_wider_window_keeps_both(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    from schwab_cli.output import dividends as div_out
    monkeypatch.setattr(div_out, "_today", lambda: date(2025, 7, 15))
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        return_value=_DIV,
    ):
        result = runner.invoke(
            app, ["dividends", "AAPL", "KO", "--upcoming", "--within-days", "90"]
        )
    assert result.exit_code == 0
    assert "AAPL" in result.output
    assert "KO" in result.output


def test_dividends_div_alias(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        return_value=_DIV,
    ):
        result = runner.invoke(app, ["div", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output


def test_dividends_both_format_flags_error(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["dividends", "AAPL", "--json", "--md"])
    assert result.exit_code == 2


def test_dividends_no_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_dividends_session_expired(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_dividends_api_error(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.quotes.get_quotes",
        side_effect=ApiError("503 bad gateway"),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1
    assert "503" in result.output


def test_dividends_brk_b_class_share(monkeypatch, tmp_path):
    """``BRK.B`` / ``BRK-B`` must surface dividend data — Schwab keys the
    response by canonical ``BRK/B``, so the renderer's per-symbol lookup
    has to match that, not the user's raw input."""
    _prep(monkeypatch, tmp_path)
    payload = {
        "BRK/B": {
            "symbol": "BRK/B",
            "quote": {"lastPrice": 450.0},
            "fundamental": {
                "dividendAmount": 0.0,
                "dividendYield": 0.0,
                "dividendFreq": 0,
            },
        }
    }
    for variant in ("BRK.B", "BRK-B", "BRK/B", "brk.b"):
        with patch(
            "schwab_cli.api.quotes.get_quotes",
            return_value=payload,
        ):
            result = runner.invoke(app, ["dividends", variant, "--json"])
        assert result.exit_code == 0, result.output
        import json
        data = json.loads(result.stdout)
        assert data[0]["symbol"] == "BRK/B"
