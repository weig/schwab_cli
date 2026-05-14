import json
import os
import stat
from pathlib import Path

import pytest

from schwab_cli.config import (
    AUTH_FLOWS,
    Config,
    ConfigError,
    config_path,
    load,
    mask_secret,
    save,
)


# ---- Config dataclass ------------------------------------------------------


def test_config_defaults():
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    assert cfg.auth_flow == "code_relay"
    assert cfg.code_relay_url is None
    assert cfg.auto_login_command is None
    assert cfg.auto_login_timeout_seconds == 300
    assert cfg.version == 1


def test_config_is_frozen():
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    with pytest.raises(Exception):
        cfg.client_id = "other"  # type: ignore[misc]


def test_config_no_longer_has_credentials_fields():
    """username / password / auto_login_enabled were removed in the auth
    refactor — credentials now live in webauto's --env file."""
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    assert not hasattr(cfg, "username")
    assert not hasattr(cfg, "password")
    assert not hasattr(cfg, "auto_login_enabled")


def test_auth_flows_includes_client_and_code_relay():
    assert AUTH_FLOWS == ("code_relay", "client")


# ---- mask_secret -----------------------------------------------------------


def test_mask_secret_long_string_shows_last_four():
    assert mask_secret("abc123xyz") == "*****3xyz"


def test_mask_secret_exactly_four_chars_fully_masked():
    assert mask_secret("abcd") == "****"


def test_mask_secret_shorter_than_four_fully_masked():
    assert mask_secret("ab") == "**"
    assert mask_secret("") == ""


# ---- config_path -----------------------------------------------------------


def test_config_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    assert config_path() == tmp_path / ".config" / "schwab_cli" / "config.json"


def test_config_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == tmp_path / "xdg" / "schwab_cli" / "config.json"


def test_config_path_override_env_wins_over_xdg_and_home(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG takes absolute precedence so a shell-level override
    can't be fooled by a stray HOME/XDG value pointing at the real file."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG", "/tmp/explicit-override.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == Path("/tmp/explicit-override.json")


def test_config_path_honors_schwab_cli_config_dir(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG_DIR points directly at the schwab_cli dir;
    config.json lives in that dir (no extra ``schwab_cli`` suffix)."""
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path / "iso"))
    assert config_path() == tmp_path / "iso" / "config.json"


def test_config_path_file_override_beats_dir_override(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG (file-level) wins over SCHWAB_CLI_CONFIG_DIR
    (dir-level). Lets you point at a specific config while still using
    the dir-default for session.json."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG", "/tmp/specific.json")
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path / "iso"))
    assert config_path() == Path("/tmp/specific.json")


# ---- load() — generic ------------------------------------------------------


def _write_config(tmp_path, data):
    """Write a config file under tmp_path/.config/schwab_cli/config.json."""
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps(data))
    return cfg_file


def _setup_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)


def test_load_returns_none_when_file_missing(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    assert load() is None


def test_load_parses_minimal_config(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    })
    cfg = load()
    assert cfg == Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="code_relay",
    )
    assert cfg.auto_login_command is None
    assert cfg.auto_login_timeout_seconds == 300


def test_load_ignores_unknown_fields(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "future_field": "ignore me",
    })
    cfg = load()
    assert cfg.client_id == "cid"


def test_load_raises_on_malformed_json(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid json")
    with pytest.raises(ConfigError, match="malformed"):
        load()


def test_load_raises_on_unsupported_future_version(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 999,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    })
    with pytest.raises(ConfigError, match="version"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    })  # no client_secret
    with pytest.raises(ConfigError, match="client_secret"):
        load()


def test_load_raises_on_missing_redirect_uri(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "auth_flow": "code_relay",
    })
    with pytest.raises(ConfigError, match="redirect_uri"):
        load()


def test_load_raises_on_missing_auth_flow(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
    })
    with pytest.raises(ConfigError, match="auth_flow"):
        load()


def test_load_raises_on_invalid_auth_flow_value(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "magic",
    })
    with pytest.raises(ConfigError, match="invalid auth_flow"):
        load()


def test_load_accepts_client_auth_flow(monkeypatch, tmp_path):
    """'client' is back in AUTH_FLOWS with new semantics (local HTTP listener)."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
    })
    cfg = load()
    assert cfg.auth_flow == "client"


def test_load_succeeds_for_code_relay_without_url(monkeypatch, tmp_path):
    """Missing ``code_relay_url`` no longer fails at load time — the check
    moved to ``auth_flows._build_handlers`` so non-auth commands can still
    use a partial config."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    })
    cfg = load()
    assert cfg.auth_flow == "code_relay"
    assert cfg.code_relay_url is None


def test_load_parses_code_relay_with_url(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://relay.example.com/u/s",
        "auth_flow": "code_relay",
        "code_relay_url": "https://relay.example.com/u/s/wait",
    })
    cfg = load()
    assert cfg.code_relay_url == "https://relay.example.com/u/s/wait"


def test_load_raises_when_root_is_not_a_dict(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("[1, 2, 3]")
    with pytest.raises(ConfigError, match="expected object at top level"):
        load()


# ---- load() — auto_login_command + timeout ---------------------------------


def test_load_parses_auto_login_command(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": [
            "webauto-cli", "/p/script.py", "--env", "/p/auto.env",
        ],
        "auto_login_timeout_seconds": 600,
    })
    cfg = load()
    assert cfg.auto_login_command == (
        "webauto-cli", "/p/script.py", "--env", "/p/auto.env",
    )
    assert cfg.auto_login_timeout_seconds == 600


def test_load_auto_login_timeout_defaults_to_300(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": ["webauto-cli", "/p/script.py"],
    })
    assert load().auto_login_timeout_seconds == 300


def test_load_rejects_auto_login_command_empty_list(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": [],
    })
    with pytest.raises(ConfigError, match="cannot be empty"):
        load()


def test_load_rejects_auto_login_command_as_string(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": "webauto-cli script.py",
    })
    with pytest.raises(ConfigError, match="must be a list of strings"):
        load()


def test_load_rejects_auto_login_command_with_non_strings(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": ["webauto-cli", 42],
    })
    with pytest.raises(ConfigError, match=r"auto_login_command\[1\]"):
        load()


def test_load_rejects_negative_timeout(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": ["webauto-cli"],
        "auto_login_timeout_seconds": -5,
    })
    with pytest.raises(ConfigError, match="must be positive"):
        load()


def test_load_rejects_zero_timeout(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": ["webauto-cli"],
        "auto_login_timeout_seconds": 0,
    })
    with pytest.raises(ConfigError, match="must be positive"):
        load()


def test_load_rejects_non_int_timeout(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
        "auto_login_command": ["webauto-cli"],
        "auto_login_timeout_seconds": "300",
    })
    with pytest.raises(ConfigError, match="must be an integer"):
        load()


# ---- save() ----------------------------------------------------------------


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    save(cfg)
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    assert file.exists()
    assert _mode(file) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    save(Config(client_id="cid", client_secret="csec",
                redirect_uri="https://127.0.0.1:8443"))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_minimal_through_load(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    original = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    save(original)
    assert load() == original


def test_save_round_trips_with_auto_login_command(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    original = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auto_login_command=("webauto-cli", "/p/script.py", "--env", "/p/auto.env"),
        auto_login_timeout_seconds=600,
    )
    save(original)
    assert load() == original


def test_save_omits_auto_login_fields_when_unset(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    save(Config(client_id="cid", client_secret="csec",
                redirect_uri="https://127.0.0.1:8443"))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "auto_login_command" not in raw
    assert "auto_login_timeout_seconds" not in raw
    # Legacy credential fields never written either.
    assert "username" not in raw
    assert "password" not in raw


def test_save_emits_auto_login_command_as_json_list(monkeypatch, tmp_path):
    """``Config.auto_login_command`` is a tuple in memory (frozen dataclass)
    but JSON has no tuple — it must serialize as a list."""
    _setup_home(monkeypatch, tmp_path)
    save(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auto_login_command=("webauto-cli", "script.py"),
    ))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert raw["auto_login_command"] == ["webauto-cli", "script.py"]


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    original = Config(client_id="orig_id", client_secret="orig_secret",
                      redirect_uri="https://127.0.0.1:8443")
    save(original)
    original_bytes = (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Config(client_id="new_id", client_secret="new_secret",
                    redirect_uri="https://127.0.0.1:8443"))

    assert (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes() == original_bytes
    strays = list((tmp_path / ".config" / "schwab_cli").glob("*.tmp"))
    assert strays == []
