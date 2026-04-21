import json
from pathlib import Path

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, load
from schwab_cli.config import save as save_cfg

runner = CliRunner()


def _run(inputs, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return runner.invoke(app, ["setup"], input=inputs)


def test_fresh_setup_without_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, decline auto-login
    result = _run("cid_value\ncsec_value\nn\n", monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid_value", client_secret="csec_value")
    assert not cfg.auto_login_enabled


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, accept auto-login, username, password
    result = _run(
        "cid_value\ncsec_value\ny\nuser@example.com\nop://Personal/Schwab/password\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid_value",
        client_secret="csec_value",
        username="user@example.com",
        password="op://Personal/Schwab/password",
    )
    assert cfg.auto_login_enabled


def test_rerun_accepting_defaults_preserves_all_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret_xyz",
            username="existing_user",
            password="op://Personal/Schwab/password",
        )
    )
    # Press Enter through every prompt: client_id, client_secret, auto-login confirm, username, password
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        username="existing_user",
        password="op://Personal/Schwab/password",
    )


def test_rerun_disabling_auto_login_removes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret",
            username="existing_user",
            password="existing_pass",
        )
    )
    # Enter for client_id, Enter for client_secret, 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False
