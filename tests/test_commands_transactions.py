import json
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
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=10_000_000_000,
                         refresh_token_expires_at=10_000_000_000))


_SAMPLE = [
    {
        "_account": "12340756",
        "activityId": 1,
        "time": "2026-04-18T14:32:11+0000",
        "type": "TRADE",
        "netAmount": -1055.30,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "AMZN"},
                "amount": 5.0, "cost": -1055.30, "price": 211.06,
                "positionEffect": "OPENING",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_transactions_default_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        return_value=_SAMPLE,
    ):
        result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 0, result.output
    assert "AMZN" in result.output


def test_transactions_default_range_is_7_days(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured = {}

    def fake(client, account_number, **kwargs):
        captured.update(kwargs)
        return _SAMPLE

    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=fake,
    ):
        result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 0, result.output
    delta = captured["end"] - captured["start"]
    assert 6.9 < delta.total_seconds() / 86400 < 7.1


def test_transactions_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        return_value=_SAMPLE,
    ):
        result = runner.invoke(app, ["transactions", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["symbol"] == "AMZN"


def test_transactions_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        return_value=_SAMPLE,
    ):
        result = runner.invoke(app, ["transactions", "--md"])
    assert result.exit_code == 0, result.output
    assert "# Transactions" in result.stdout


def test_transactions_custom_range(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured = {}

    def fake(client, account_number, **kwargs):
        captured.update(kwargs)
        return _SAMPLE

    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=fake,
    ):
        result = runner.invoke(app, [
            "transactions", "--range=20260101..20260430",
        ])
    assert result.exit_code == 0, result.output
    assert captured["start"].date().isoformat() == "2026-01-01"


def test_transactions_type_all_filters_locally(monkeypatch, tmp_path):
    """``--type=ALL`` is applied locally now (cache always fetches the
    full set with no Schwab-side type filter)."""
    _prep(monkeypatch, tmp_path)
    captured = {}

    def fake(client, account_number, **kwargs):
        captured.update(kwargs)
        return _SAMPLE

    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=fake,
    ):
        result = runner.invoke(app, ["transactions", "--type=ALL"])
    assert result.exit_code == 0, result.output
    # fetch_cached doesn't get a types kwarg — filtering happens locally.
    assert "types" not in captured
    assert "type_filter" not in captured


def test_transactions_specific_account_via_flag(monkeypatch, tmp_path):
    """``--account 0756`` (or ``-a 0756``) replaces the old positional form."""
    _prep(monkeypatch, tmp_path)
    captured = {}

    def fake(client, account_number, **kwargs):
        captured["account"] = account_number
        return _SAMPLE

    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=fake,
    ):
        result = runner.invoke(app, ["transactions", "--account", "0756"])
    assert result.exit_code == 0, result.output
    assert captured["account"] == "0756"


def test_transactions_empty_is_ok(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        return_value=[],
    ):
        result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Error matrix
# ---------------------------------------------------------------------------

def test_transactions_json_md_mutex_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["transactions", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_transactions_invalid_range_grammar_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["transactions", "--range=garbage"])
    assert result.exit_code == 2


def test_transactions_inverted_range_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, [
        "transactions", "--range=20260601..20260101",
    ])
    assert result.exit_code == 1
    assert "before end" in result.output


def test_transactions_no_config_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 1
    assert "No config" in result.output


def test_transactions_no_session_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_transactions_session_expired_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_transactions_api_error_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.transactions.fetch_cached",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 1
    assert "503" in result.output
