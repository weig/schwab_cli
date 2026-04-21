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
