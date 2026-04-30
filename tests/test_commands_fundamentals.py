from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
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
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    ))


_FUND = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14},
        "fundamental": {
            "peRatio": 33.85,
            "pegRatio": 3.21,
            "epsTTM": 6.54,
            "marketCap": 3.43e12,
            "dividendYield": 0.44,
            "beta": 1.25,
            "high52": 260.10,
            "low52": 164.08,
        },
    }
}


def test_fundamentals_requests_fundamental_field(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        return_value=_FUND,
    ) as mock:
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 0, result.output
    _, kwargs = mock.call_args
    assert kwargs.get("fields") == "all"
    assert "AAPL" in result.output


def test_fundamentals_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        return_value=_FUND,
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    import json
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["fundamental"]["peRatio"] == 33.85


def test_fundamentals_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        return_value=_FUND,
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--md"])
    assert result.exit_code == 0
    assert "| Symbol" in result.stdout


def test_fundamentals_multi_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    payload = {
        "AAPL": _FUND["AAPL"],
        "MSFT": {
            "symbol": "MSFT",
            "quote": {"lastPrice": 450.0},
            "fundamental": {"peRatio": 35.0, "epsTTM": 12.9},
        },
    }
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        return_value=payload,
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL", "MSFT"])
    assert result.exit_code == 0
    assert "AAPL" in result.output
    assert "MSFT" in result.output


def test_fundamentals_both_flags_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["fundamentals", "AAPL", "--json", "--md"])
    assert result.exit_code == 2


def test_fundamentals_no_symbol_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["fundamentals"])
    assert result.exit_code != 0


def test_fundamentals_no_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_fundamentals_session_expired(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_fundamentals_api_error(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.fundamentals.get_quotes",
        side_effect=ApiError("500 internal"),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1
    assert "500" in result.output


def test_fundamentals_brk_b_class_share(monkeypatch, tmp_path):
    """Class-share inputs ``BRK.B`` / ``BRK-B`` must surface the same data
    as the canonical ``BRK/B`` form. Regression: the API request was
    normalized but the renderer still keyed the response by the raw user
    input, so values came back blank."""
    _prep(monkeypatch, tmp_path)
    payload = {
        "BRK/B": {
            "symbol": "BRK/B",
            "quote": {"lastPrice": 450.0},
            "fundamental": {"peRatio": 9.5, "epsTTM": 47.4},
        }
    }
    for variant in ("BRK.B", "BRK-B", "BRK/B", "brk.b"):
        with patch(
            "schwab_cli.commands.fundamentals.get_quotes",
            return_value=payload,
        ):
            result = runner.invoke(app, ["fundamentals", variant, "--json"])
        assert result.exit_code == 0, result.output
        import json
        data = json.loads(result.stdout)
        assert data[0]["symbol"] == "BRK/B"
        assert data[0]["fundamental"]["peRatio"] == 9.5
