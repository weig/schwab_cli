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
