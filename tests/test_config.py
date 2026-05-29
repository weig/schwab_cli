"""Tests for :mod:`schwab_cli.config`.

Post-refactor contract:
  - AUTH_FLOWS = ("local_server",)  — only new valid flow
  - _LEGACY_FLOWS contains "code_relay" and "client"
  - load() accepts legacy auth_flow values WITHOUT raising (H4 regression)
  - load() raises ConfigError for truly unknown auth_flow values
  - Config default auth_flow = "local_server"
  - code_relay_url field IS REMOVED from Config
  - to_payload() never emits code_relay_url
  - Config file containing code_relay_url key loads fine (key ignored)
"""
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


def test_config_default_auth_flow_is_local_server():
    """After the refactor, the default auth_flow must be 'local_server'."""
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    assert cfg.auth_flow == "local_server"


def test_config_does_not_accept_code_relay_url_kwarg():
    """code_relay_url field is removed from Config — passing it raises TypeError."""
    with pytest.raises(TypeError):
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
            code_relay_url="https://relay.example.com/wait",  # type: ignore[call-arg]
        )


def test_config_has_no_code_relay_url_attribute():
    """Config instances must not carry a code_relay_url attribute."""
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    assert not hasattr(cfg, "code_relay_url")


def test_config_defaults():
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:8443")
    assert cfg.auth_flow == "local_server"
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


def test_auth_flows_contains_only_local_server():
    """AUTH_FLOWS must contain only the new local_server flow."""
    assert AUTH_FLOWS == ("local_server",)


def test_auth_flows_does_not_contain_code_relay():
    assert "code_relay" not in AUTH_FLOWS


def test_auth_flows_does_not_contain_client():
    assert "client" not in AUTH_FLOWS


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


def test_load_parses_local_server_config(monkeypatch, tmp_path):
    """local_server round-trips through load."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
    })
    cfg = load()
    assert cfg == Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    assert cfg.auto_login_command is None
    assert cfg.auto_login_timeout_seconds == 300


def test_load_ignores_unknown_fields(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
        "future_field": "ignore me",
    })
    cfg = load()
    assert cfg.client_id == "cid"


# ---- load() — H4 regression: legacy code_relay config tolerating ----------


def test_load_tolerates_legacy_code_relay_config(monkeypatch, tmp_path):
    """H4 regression: a production-shaped legacy config with auth_flow='code_relay'
    must load WITHOUT raising ConfigError, so non-auth commands keep working.
    The returned Config must preserve auth_flow='code_relay'."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://relay.example.com/uuid/callback",
        "auth_flow": "code_relay",
        "code_relay_url": "https://relay.example.com/uuid/wait",
    })
    cfg = load()  # must NOT raise
    assert cfg is not None
    assert cfg.auth_flow == "code_relay"
    # code_relay_url in the file is ignored — no attribute on Config
    assert not hasattr(cfg, "code_relay_url")


def test_load_tolerates_legacy_code_relay_without_relay_url(monkeypatch, tmp_path):
    """Legacy code_relay config with no code_relay_url also loads fine."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://relay.example.com/uuid/callback",
        "auth_flow": "code_relay",
    })
    cfg = load()
    assert cfg is not None
    assert cfg.auth_flow == "code_relay"


def test_load_tolerates_legacy_client_auth_flow(monkeypatch, tmp_path):
    """'client' is a legacy flow — load() must accept it without raising."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
    })
    cfg = load()
    assert cfg is not None
    assert cfg.auth_flow == "client"


def test_load_raises_on_truly_unknown_auth_flow(monkeypatch, tmp_path):
    """A truly unrecognized auth_flow (neither new nor legacy) must raise ConfigError."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "bogus",
    })
    with pytest.raises(ConfigError, match="invalid auth_flow"):
        load()


# ---- load() — error cases --------------------------------------------------


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
        "auth_flow": "local_server",
    })
    with pytest.raises(ConfigError, match="version"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "local_server",
    })  # no client_secret
    with pytest.raises(ConfigError, match="client_secret"):
        load()


def test_load_raises_on_missing_redirect_uri(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "auth_flow": "local_server",
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
    """'magic' is neither a valid new flow nor a legacy flow."""
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


def test_load_raises_when_root_is_not_a_dict(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("[1, 2, 3]")
    with pytest.raises(ConfigError, match="expected object at top level"):
        load()


def test_load_ignores_code_relay_url_key_in_file(monkeypatch, tmp_path):
    """A config file that still has code_relay_url key loads fine; key is ignored."""
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
        "code_relay_url": "https://relay.example.com/wait",  # legacy key, ignored
    })
    cfg = load()
    assert cfg is not None
    assert cfg.auth_flow == "local_server"
    assert not hasattr(cfg, "code_relay_url")


# ---- load() — auto_login_command + timeout ---------------------------------


def test_load_parses_auto_login_command(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
        "auto_login_command": ["webauto-cli", "/p/script.py"],
    })
    assert load().auto_login_timeout_seconds == 300


def test_load_rejects_auto_login_command_empty_list(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
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
        "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
        "auth_flow": "local_server",
        "auto_login_command": ["webauto-cli"],
        "auto_login_timeout_seconds": "300",
    })
    with pytest.raises(ConfigError, match="must be an integer"):
        load()


# ---- to_payload() — never emits code_relay_url ----------------------------


def test_to_payload_never_contains_code_relay_url():
    """Regardless of the auth_flow, to_payload() must not emit code_relay_url."""
    cfg = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    payload = cfg.to_payload()
    assert "code_relay_url" not in payload


def test_to_payload_local_server_emits_correct_auth_flow():
    cfg = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    payload = cfg.to_payload()
    assert payload["auth_flow"] == "local_server"


# ---- save() ----------------------------------------------------------------


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    cfg = Config(client_id="cid", client_secret="csec",
                 redirect_uri="https://127.0.0.1:19806/schwab/callback")
    save(cfg)
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    assert file.exists()
    assert _mode(file) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    save(Config(client_id="cid", client_secret="csec",
                redirect_uri="https://127.0.0.1:19806/schwab/callback"))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_local_server_through_load(monkeypatch, tmp_path):
    """local_server config saves and loads correctly."""
    _setup_home(monkeypatch, tmp_path)
    original = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    save(original)
    assert load() == original


def test_save_round_trips_with_auto_login_command(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    original = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
        auto_login_command=("webauto-cli", "/p/script.py", "--env", "/p/auto.env"),
        auto_login_timeout_seconds=600,
    )
    save(original)
    assert load() == original


def test_save_omits_auto_login_fields_when_unset(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    save(Config(client_id="cid", client_secret="csec",
                redirect_uri="https://127.0.0.1:19806/schwab/callback"))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "auto_login_command" not in raw
    assert "auto_login_timeout_seconds" not in raw
    assert "username" not in raw
    assert "password" not in raw
    assert "code_relay_url" not in raw


def test_save_never_writes_code_relay_url(monkeypatch, tmp_path):
    """to_payload() must never emit code_relay_url regardless of inputs."""
    _setup_home(monkeypatch, tmp_path)
    cfg = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    save(cfg)
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "code_relay_url" not in raw


def test_save_emits_auto_login_command_as_json_list(monkeypatch, tmp_path):
    """``Config.auto_login_command`` is a tuple in memory (frozen dataclass)
    but JSON has no tuple — it must serialize as a list."""
    _setup_home(monkeypatch, tmp_path)
    save(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
        auto_login_command=("webauto-cli", "script.py"),
    ))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert raw["auto_login_command"] == ["webauto-cli", "script.py"]


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    _setup_home(monkeypatch, tmp_path)
    original = Config(client_id="orig_id", client_secret="orig_secret",
                      redirect_uri="https://127.0.0.1:19806/schwab/callback")
    save(original)
    original_bytes = (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Config(client_id="new_id", client_secret="new_secret",
                    redirect_uri="https://127.0.0.1:19806/schwab/callback"))

    assert (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes() == original_bytes
    strays = list((tmp_path / ".config" / "schwab_cli").glob("*.tmp"))
    assert strays == []
