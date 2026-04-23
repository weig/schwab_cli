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
    # client_id, client_secret, redirect_uri, auth_flow (Enter→default 'client'), decline auto-login
    result = _run("cid_value\ncsec_value\nhttps://127.0.0.1:8443\n\nn\n", monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid_value", client_secret="csec_value", redirect_uri="https://127.0.0.1:8443")
    assert not cfg.auto_login_enabled


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, redirect_uri, auth_flow (default), accept auto-login, username, password
    result = _run(
        "cid_value\ncsec_value\nhttps://127.0.0.1:8443\n\ny\nuser@example.com\nop://Personal/Schwab/password\n",
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


def test_fresh_setup_with_code_relay_flow(monkeypatch, tmp_path):
    # client_id, client_secret, redirect_uri, auth_flow=code_relay, code_relay_url, decline auto-login
    relay = "https://relay.example.com/uuid/secret/wait"
    result = _run(
        f"cid\ncsec\nhttps://relay.example.com/uuid/secret\ncode_relay\n{relay}\nn\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auth_flow == "code_relay"
    assert cfg.code_relay_url == relay


def test_fresh_setup_reprompts_on_invalid_auth_flow(monkeypatch, tmp_path):
    # bad auth_flow value triggers reprompt; second time passes
    result = _run(
        "cid\ncsec\nhttps://127.0.0.1:8443\nbogus\nclient\nn\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "Auth flow must be one of" in result.output
    cfg = load()
    assert cfg.auth_flow == "client"


def test_fresh_setup_auth_flow_by_number_picks_code_relay(monkeypatch, tmp_path):
    # Selecting "2" should map to the second AUTH_FLOWS entry (code_relay).
    # Follow-up prompts the relay /wait URL, then declines auto-login.
    relay = "https://relay.example.com/uuid/secret/wait"
    result = _run(
        f"cid\ncsec\nhttps://relay.example.com/uuid/secret\n2\n{relay}\nn\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auth_flow == "code_relay"
    assert cfg.code_relay_url == relay


def test_dry_run_prints_payload_and_does_not_save(monkeypatch, tmp_path):
    """--dry-run runs the prompts but prints JSON instead of writing the file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input="cid_value\ncsec_value\nhttps://127.0.0.1:8443\n\nn\n",
    )
    assert result.exit_code == 0, result.output

    # No file on disk.
    assert load() is None
    assert not (tmp_path / ".config" / "schwab_cli" / "config.json").exists()

    # Output carries the dry-run banner and the JSON payload the `save`
    # path would have written.
    assert "dry-run" in result.output.lower()
    assert '"client_id": "cid_value"' in result.output
    assert '"client_secret": "csec_value"' in result.output
    assert '"auth_flow": "client"' in result.output


def test_dry_run_does_not_overwrite_existing_config(monkeypatch, tmp_path):
    """Existing config bytes survive untouched after a --dry-run session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    original_bytes = file.read_bytes()

    # Type new values for every prompt so the dry-run payload differs from
    # the on-disk config; decline auto-login.
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input="new_id\nnew_secret\nhttps://127.0.0.1:9999\n\nn\n",
    )
    assert result.exit_code == 0, result.output

    # File is bit-for-bit unchanged.
    assert file.read_bytes() == original_bytes
    assert load().client_id == "existing_id"

    # But the dry-run printout reflects the NEW values we typed.
    assert '"client_id": "new_id"' in result.output
    assert '"redirect_uri": "https://127.0.0.1:9999"' in result.output


def test_setup_shows_auth_flow_descriptions(monkeypatch, tmp_path):
    """The menu must describe each auth_flow so the user can pick informedly."""
    result = _run(
        "cid\ncsec\nhttps://127.0.0.1:8443\n\nn\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    # Each flow name + a distinctive phrase from its description appears.
    assert "client" in result.output
    assert "loopback redirect_uri" in result.output
    assert "code_relay" in result.output
    assert "public relay" in result.output


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
    # Press Enter through every prompt: client_id, client_secret, redirect_uri, auth_flow,
    # auto-login confirm, username, password
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n\n")
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
    # Enter for client_id, client_secret, redirect_uri, auth_flow, then 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False


def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # First: empty client_id (should re-prompt), then valid one, client_secret, redirect_uri,
    # auth_flow (default), decline auto.
    result = runner.invoke(app, ["setup"], input="\ncid_value\ncsec_value\nhttps://127.0.0.1:8443\n\nn\n")
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

    # y = overwrite, then client_id, client_secret, redirect_uri, auth_flow (default), decline auto-login
    result = runner.invoke(app, ["setup"], input="y\ncid\ncsec\nhttps://127.0.0.1:8443\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
