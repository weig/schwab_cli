import json

import pytest

from schwab_cli.config import Config


def test_config_defaults_username_password_to_none():
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.version == 1


def test_config_is_frozen():
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    with pytest.raises(Exception):
        cfg.client_id = "other"  # type: ignore[misc]


def test_auto_login_enabled_requires_both_fields():
    both = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443", username="u", password="p")
    only_user = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443", username="u")
    only_pass = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443", password="p")
    neither = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")

    assert both.auto_login_enabled is True
    assert only_user.auto_login_enabled is False
    assert only_pass.auto_login_enabled is False
    assert neither.auto_login_enabled is False


from schwab_cli.config import mask_secret


def test_mask_secret_long_string_shows_last_four():
    assert mask_secret("abc123xyz") == "*****3xyz"


def test_mask_secret_exactly_four_chars_fully_masked():
    assert mask_secret("abcd") == "****"


def test_mask_secret_shorter_than_four_fully_masked():
    assert mask_secret("ab") == "**"
    assert mask_secret("") == ""


from pathlib import Path

from schwab_cli.config import config_path


def test_config_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config_path() == tmp_path / ".config" / "schwab_cli" / "config.json"


def test_config_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == tmp_path / "xdg" / "schwab_cli" / "config.json"


def test_config_path_override_env_wins_over_xdg_and_home(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG takes absolute precedence so a shell-level override
    can't be fooled by a stray HOME/XDG value pointing at the real file."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG", "/tmp/explicit-override.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == Path("/tmp/explicit-override.json")


from schwab_cli.config import ConfigError, load


def _write_config(tmp_path, data):
    """Write a config file under tmp_path/.config/schwab_cli/config.json."""
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps(data))
    return cfg_file


def test_load_returns_none_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert load() is None


def test_load_parses_full_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
        "username": "u",
        "password": "op://Personal/Schwab/password",
    })
    cfg = load()
    assert cfg == Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="client",
        username="u",
        password="op://Personal/Schwab/password",
    )
    assert cfg.auto_login_enabled is True


def test_load_parses_minimal_config_without_auto_login(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
    })
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False


def test_load_ignores_unknown_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
        "future_field": "ignore me",
    })
    cfg = load()
    assert cfg.client_id == "cid"


def test_load_raises_on_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid json")
    with pytest.raises(ConfigError, match="malformed"):
        load()


def test_load_raises_on_unsupported_future_version(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 999,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
    })
    with pytest.raises(ConfigError, match="version"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "client",
    })  # no client_secret
    with pytest.raises(ConfigError, match="client_secret"):
        load()


def test_load_raises_on_missing_redirect_uri(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "auth_flow": "client",
    })
    with pytest.raises(ConfigError, match="redirect_uri"):
        load()


def test_load_raises_on_missing_auth_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
    })
    with pytest.raises(ConfigError, match="auth_flow"):
        load()


def test_load_raises_on_invalid_auth_flow_value(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "magic",
    })
    with pytest.raises(ConfigError, match="invalid auth_flow"):
        load()


def test_load_raises_on_code_relay_without_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://127.0.0.1:8443",
        "auth_flow": "code_relay",
    })
    with pytest.raises(ConfigError, match="code_relay_url"):
        load()


def test_load_parses_code_relay_with_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "https://relay.example.com/u/s",
        "auth_flow": "code_relay",
        "code_relay_url": "https://relay.example.com/u/s/wait",
    })
    cfg = load()
    assert cfg.auth_flow == "code_relay"
    assert cfg.code_relay_url == "https://relay.example.com/u/s/wait"


def test_load_raises_when_root_is_not_a_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("[1, 2, 3]")
    with pytest.raises(ConfigError, match="expected object at top level"):
        load()


import os
import stat

from schwab_cli.config import save


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    save(cfg)
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    assert file.exists()
    assert _mode(file) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_through_load(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    original = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        username="u",
        password="op://Personal/Schwab/password",
    )
    save(original)
    assert load() == original


def test_save_omits_none_username_and_password(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "username" not in raw
    assert "password" not in raw


def test_save_disabling_auto_login_removes_prior_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443", username="u", password="p"))
    save(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))  # disables auto-login
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "username" not in raw
    assert "password" not in raw


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # First write establishes a known-good file.
    original = Config(client_id="orig_id", client_secret="orig_secret", redirect_uri="https://127.0.0.1:8443")
    save(original)
    original_bytes = (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes()

    # Break os.replace to simulate a crash between temp-write and rename.
    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Config(client_id="new_id", client_secret="new_secret", redirect_uri="https://127.0.0.1:8443"))

    # Original file untouched.
    assert (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes() == original_bytes
    # No stray .tmp left behind.
    strays = list((tmp_path / ".config" / "schwab_cli").glob("*.tmp"))
    assert strays == []
