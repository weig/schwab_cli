import json

import pytest

from schwab_cli.config import Config


def test_config_defaults_username_password_to_none():
    cfg = Config(client_id="cid", client_secret="csec")
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.version == 1


def test_config_is_frozen():
    cfg = Config(client_id="cid", client_secret="csec")
    with pytest.raises(Exception):
        cfg.client_id = "other"  # type: ignore[misc]


def test_auto_login_enabled_requires_both_fields():
    both = Config(client_id="cid", client_secret="csec", username="u", password="p")
    only_user = Config(client_id="cid", client_secret="csec", username="u")
    only_pass = Config(client_id="cid", client_secret="csec", password="p")
    neither = Config(client_id="cid", client_secret="csec")

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
        "username": "u",
        "password": "op://Personal/Schwab/password",
    })
    cfg = load()
    assert cfg == Config(
        client_id="cid",
        client_secret="csec",
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
    })
    with pytest.raises(ConfigError, match="version"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {"version": 1, "client_id": "cid"})  # no client_secret
    with pytest.raises(ConfigError, match="client_secret"):
        load()


def test_load_raises_when_root_is_not_a_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("[1, 2, 3]")
    with pytest.raises(ConfigError, match="expected object at top level"):
        load()
