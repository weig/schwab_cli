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
    # client_id, client_secret, redirect_uri, decline auto-login
    result = _run("cid_value\ncsec_value\nhttps://127.0.0.1:8443\nn\n", monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid_value", client_secret="csec_value", redirect_uri="https://127.0.0.1:8443")
    assert not cfg.auto_login_enabled


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, redirect_uri, accept auto-login, username, password
    result = _run(
        "cid_value\ncsec_value\nhttps://127.0.0.1:8443\ny\nuser@example.com\nop://Personal/Schwab/password\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid_value",
        client_secret="csec_value",
        redirect_uri="https://127.0.0.1:8443",
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
            redirect_uri="https://127.0.0.1:8443",
            username="existing_user",
            password="op://Personal/Schwab/password",
        )
    )
    # Press Enter through every prompt: client_id, client_secret, redirect_uri, auto-login confirm, username, password
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri="https://127.0.0.1:8443",
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
            redirect_uri="https://127.0.0.1:8443",
            username="existing_user",
            password="existing_pass",
        )
    )
    # Enter for client_id, Enter for client_secret, Enter for redirect_uri, 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False


def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # First: empty client_id (should re-prompt), then valid one, client_secret, redirect_uri, decline auto.
    result = runner.invoke(app, ["setup"], input="\ncid_value\ncsec_value\nhttps://127.0.0.1:8443\nn\n")
    assert result.exit_code == 0, result.output
    assert "Client ID is required" in result.output
    cfg = load()
    assert cfg.client_id == "cid_value"
    assert cfg.redirect_uri == "https://127.0.0.1:8443"


def test_malformed_existing_config_decline_overwrite_leaves_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    bad = cfg_dir / "config.json"
    bad.write_text("{not valid")
    original_bytes = bad.read_bytes()

    result = runner.invoke(app, ["setup"], input="n\n")  # decline overwrite
    assert result.exit_code == 0, result.output
    assert bad.read_bytes() == original_bytes


def test_malformed_existing_config_accept_overwrite_writes_new(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid")

    # y = overwrite, then client_id, client_secret, redirect_uri, decline auto-login
    result = runner.invoke(app, ["setup"], input="y\ncid\ncsec\nhttps://127.0.0.1:8443\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
