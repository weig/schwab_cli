"""Tests for ``schwab_cli setup``.

The auth_flow prompt was removed after the refactor — there's only one
choice today (``code_relay``). Setup now prompts:

  1. client_id
  2. client_secret
  3. redirect_uri
  4. code_relay_url
  5. enable auto-login? (y/n)
  6. (if y) username
  7. (if y) password

Tests below feed those values via stdin and assert the resulting
``config.json``.
"""
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, load
from schwab_cli.config import save as save_cfg

runner = CliRunner()

_RELAY_URL = "https://relay.example.com/uuid/secret/wait"
_REDIRECT_URI = "https://relay.example.com/uuid/secret"


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)


def _run(inputs, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    return runner.invoke(app, ["setup"], input=inputs)


def test_fresh_setup_without_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, redirect_uri, code_relay_url, decline auto-login
    result = _run(
        f"cid_value\ncsec_value\n{_REDIRECT_URI}\n{_RELAY_URL}\nn\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid_value",
        client_secret="csec_value",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
    )
    assert not cfg.auto_login_enabled


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, redirect_uri, code_relay_url, accept auto-login,
    # username, password
    result = _run(
        f"cid_value\ncsec_value\n{_REDIRECT_URI}\n{_RELAY_URL}\ny\n"
        f"user@example.com\nmy_password\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid_value",
        client_secret="csec_value",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
        username="user@example.com",
        password="my_password",
    )
    assert cfg.auto_login_enabled


def test_dry_run_prints_payload_and_does_not_save(monkeypatch, tmp_path):
    """--dry-run runs the prompts but prints JSON instead of writing the file."""
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input=f"cid_value\ncsec_value\n{_REDIRECT_URI}\n{_RELAY_URL}\nn\n",
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
    assert '"auth_flow": "code_relay"' in result.output


def test_dry_run_does_not_overwrite_existing_config(monkeypatch, tmp_path):
    """Existing config bytes survive untouched after a --dry-run session."""
    _setup_env(monkeypatch, tmp_path)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret",
            redirect_uri=_REDIRECT_URI,
            auth_flow="code_relay",
            code_relay_url=_RELAY_URL,
        )
    )
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    original_bytes = file.read_bytes()

    new_redirect = "https://relay.example.com/v2/uuid/secret"
    new_relay = "https://relay.example.com/v2/uuid/secret/wait"
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input=f"new_id\nnew_secret\n{new_redirect}\n{new_relay}\nn\n",
    )
    assert result.exit_code == 0, result.output

    # File is bit-for-bit unchanged.
    assert file.read_bytes() == original_bytes
    assert load().client_id == "existing_id"

    # But the dry-run printout reflects the NEW values we typed.
    assert '"client_id": "new_id"' in result.output
    assert f'"redirect_uri": "{new_redirect}"' in result.output


def test_rerun_accepting_defaults_preserves_all_values(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret_xyz",
            redirect_uri=_REDIRECT_URI,
            auth_flow="code_relay",
            code_relay_url=_RELAY_URL,
            username="existing_user",
            password="existing_pass",
        )
    )
    # Press Enter through every prompt: client_id, client_secret,
    # redirect_uri, code_relay_url, auto-login confirm, username, password
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
        username="existing_user",
        password="existing_pass",
    )


def test_rerun_disabling_auto_login_removes_credentials(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret",
            redirect_uri=_REDIRECT_URI,
            auth_flow="code_relay",
            code_relay_url=_RELAY_URL,
            username="existing_user",
            password="existing_pass",
        )
    )
    # Enter for client_id, client_secret, redirect_uri, code_relay_url,
    # then 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False


def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    # First: empty client_id (should re-prompt), then valid client_id,
    # then client_secret, redirect_uri, code_relay_url, decline auto.
    result = runner.invoke(
        app, ["setup"],
        input=f"\ncid_value\ncsec_value\n{_REDIRECT_URI}\n{_RELAY_URL}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Client ID is required" in result.output
    cfg = load()
    assert cfg.client_id == "cid_value"
    assert cfg.code_relay_url == _RELAY_URL


def test_malformed_existing_config_decline_overwrite_leaves_file(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    bad = cfg_dir / "config.json"
    bad.write_text("{not valid")
    original_bytes = bad.read_bytes()

    result = runner.invoke(app, ["setup"], input="n\n")  # decline overwrite
    assert result.exit_code == 0, result.output
    assert bad.read_bytes() == original_bytes


def test_malformed_existing_config_accept_overwrite_writes_new(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid")

    # y = overwrite, then client_id, client_secret, redirect_uri,
    # code_relay_url, decline auto-login
    result = runner.invoke(
        app, ["setup"],
        input=f"y\ncid\ncsec\n{_REDIRECT_URI}\n{_RELAY_URL}\nn\n",
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
    )
