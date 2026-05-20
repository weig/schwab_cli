"""Tests for ``schwab_cli setup``.

Prompts in order:

  1. client_id
  2. client_secret
  3. redirect_uri
  4. auth_flow                       (code_relay | client)
  5. code_relay_url                  (only when auth_flow=code_relay)
  6. Configure auto-login? (y/n)
  7. (if y) auto_login_command       (parsed via shlex.split)
  8. (if y) auto_login_timeout_seconds
"""
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, load
from schwab_cli.config import save as save_cfg

runner = CliRunner()

_RELAY_URL = "https://relay.example.com/uuid/secret/wait"
_REDIRECT_URI = "https://relay.example.com/uuid/secret"
_AUTO_CMD = "webauto-cli /p/script.py --env /p/auto.env"
_AUTO_CMD_TUPLE = ("webauto-cli", "/p/script.py", "--env", "/p/auto.env")


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)


def _run(inputs, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    return runner.invoke(app, ["setup"], input=inputs)


def test_fresh_setup_code_relay_without_auto_login(monkeypatch, tmp_path):
    """code_relay flow, decline auto-login."""
    # client_id, secret, redirect, auth_flow, code_relay_url, "n" auto-login
    result = _run(
        f"cid_value\ncsec_value\n{_REDIRECT_URI}\n"
        f"code_relay\n{_RELAY_URL}\nn\n",
        monkeypatch, tmp_path,
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
    assert cfg.auto_login_command is None
    assert cfg.auto_login_timeout_seconds == 300


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    result = _run(
        f"cid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\n"
        f"y\n{_AUTO_CMD}\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auto_login_command == _AUTO_CMD_TUPLE
    assert cfg.auto_login_timeout_seconds == 300


def test_fresh_setup_auth_flow_client(monkeypatch, tmp_path):
    """client flow (local listener) — no code_relay_url prompt."""
    # cid, secret, redirect, auth_flow=client, decline auto-login
    result = _run(
        "cid\ncsec\nhttps://127.0.0.1:8443\nclient\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auth_flow == "client"
    assert cfg.code_relay_url is None


def test_fresh_setup_reprompts_on_invalid_auth_flow(monkeypatch, tmp_path):
    """Bad auth_flow re-prompts; second time passes."""
    result = _run(
        f"cid\ncsec\n{_REDIRECT_URI}\nbogus\ncode_relay\n{_RELAY_URL}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "Must be one of" in result.output
    assert load().auth_flow == "code_relay"


def test_setup_auto_login_command_with_quoted_path(monkeypatch, tmp_path):
    """``shlex.split`` handles quoted paths with spaces."""
    quoted = 'webauto-cli "/path with spaces/script.py" --env /p/env'
    result = _run(
        f"cid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\n"
        f"y\n{quoted}\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auto_login_command == (
        "webauto-cli", "/path with spaces/script.py", "--env", "/p/env",
    )


def test_setup_auto_login_reprompts_on_invalid_timeout(monkeypatch, tmp_path):
    result = _run(
        f"cid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\n"
        f"y\n{_AUTO_CMD}\n-5\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "positive integer" in result.output
    assert load().auto_login_timeout_seconds == 300


def test_setup_auto_login_keeps_default_timeout_on_empty_input(monkeypatch, tmp_path):
    """Empty timeout input → keep default (300)."""
    result = _run(
        f"cid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\n"
        f"y\n{_AUTO_CMD}\n\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert load().auto_login_timeout_seconds == 300


def test_dry_run_prints_payload_and_does_not_save(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input=f"cid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert load() is None
    assert "dry-run" in result.output.lower()
    assert '"auth_flow": "code_relay"' in result.output


def test_dry_run_does_not_overwrite_existing_config(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    save_cfg(Config(
        client_id="existing_id", client_secret="existing_secret",
        redirect_uri=_REDIRECT_URI, auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
    ))
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    original_bytes = file.read_bytes()

    new_redirect = "https://relay.example.com/v2/uuid/secret"
    new_relay = "https://relay.example.com/v2/uuid/secret/wait"
    result = runner.invoke(
        app, ["setup", "--dry-run"],
        input=f"new_id\nnew_secret\n{new_redirect}\ncode_relay\n{new_relay}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert file.read_bytes() == original_bytes
    assert load().client_id == "existing_id"
    assert '"client_id": "new_id"' in result.output


def test_rerun_accepting_defaults_preserves_all_values(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    save_cfg(Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
        auto_login_command=_AUTO_CMD_TUPLE,
        auto_login_timeout_seconds=600,
    ))
    # Press Enter through: client_id, client_secret, redirect_uri, auth_flow,
    # code_relay_url, auto-login confirm (default y), command (Enter keeps),
    # timeout (Enter keeps default).
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri=_REDIRECT_URI,
        auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
        auto_login_command=_AUTO_CMD_TUPLE,
        auto_login_timeout_seconds=600,
    )


def test_rerun_disabling_auto_login_removes_command(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    save_cfg(Config(
        client_id="cid", client_secret="csec",
        redirect_uri=_REDIRECT_URI, auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
        auto_login_command=_AUTO_CMD_TUPLE,
    ))
    # Press Enter through cid/secret/redirect/auth_flow/code_relay_url,
    # then 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auto_login_command is None


def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["setup"],
        input=f"\ncid_value\ncsec_value\n{_REDIRECT_URI}\n"
              f"code_relay\n{_RELAY_URL}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Client ID is required" in result.output
    assert load().client_id == "cid_value"


def test_malformed_existing_config_decline_overwrite_leaves_file(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    bad = cfg_dir / "config.json"
    bad.write_text("{not valid")
    original_bytes = bad.read_bytes()

    result = runner.invoke(app, ["setup"], input="n\n")
    assert result.exit_code == 0, result.output
    assert bad.read_bytes() == original_bytes


def test_malformed_existing_config_accept_overwrite_writes_new(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid")

    result = runner.invoke(
        app, ["setup"],
        input=f"y\ncid\ncsec\n{_REDIRECT_URI}\ncode_relay\n{_RELAY_URL}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert load() == Config(
        client_id="cid", client_secret="csec",
        redirect_uri=_REDIRECT_URI, auth_flow="code_relay",
        code_relay_url=_RELAY_URL,
    )
