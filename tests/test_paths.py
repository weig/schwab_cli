"""Tests for `schwab_cli.paths.config_dir()`.

The resolver decides where ``config.json`` and ``session.json`` live.
Precedence:
  1. ``SCHWAB_CLI_CONFIG_DIR`` — direct path (no ``schwab_cli`` suffix appended).
  2. ``XDG_CONFIG_HOME/schwab_cli`` — XDG convention.
  3. ``~/.config/schwab_cli`` — fallback.
"""
from __future__ import annotations

from pathlib import Path

from schwab_cli.paths import ENV_CONFIG_DIR, config_dir


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv(ENV_CONFIG_DIR, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_defaults_to_home_config_schwab_cli(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "schwab_cli"


def test_xdg_overrides_default(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "schwab_cli"


def test_schwab_cli_config_dir_overrides_xdg(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "override"))
    assert config_dir() == tmp_path / "override"


def test_schwab_cli_config_dir_no_suffix_appended(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG_DIR points DIRECTLY at the schwab_cli dir.

    Unlike XDG_CONFIG_HOME, no ``schwab_cli`` suffix is appended. This is
    deliberate so test setups can use any directory name.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "my_test_dir"))
    assert config_dir() == tmp_path / "my_test_dir"
    # Negative: must NOT append "schwab_cli"
    assert config_dir() != tmp_path / "my_test_dir" / "schwab_cli"


def test_schwab_cli_config_dir_expands_tilde(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(ENV_CONFIG_DIR, "~/test_dir")
    assert config_dir() == tmp_path / "test_dir"


def test_empty_override_falls_through(monkeypatch, tmp_path):
    """Empty string for SCHWAB_CLI_CONFIG_DIR should not be treated as
    a path — fall through to the next layer. Defends against
    ``SCHWAB_CLI_CONFIG_DIR= schwab_cli auth`` accidentally pointing at cwd.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(ENV_CONFIG_DIR, "")
    assert config_dir() == tmp_path / ".config" / "schwab_cli"


def test_returns_path_object(monkeypatch, tmp_path):
    """Type contract: config_dir() returns a pathlib.Path."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path))
    result = config_dir()
    assert isinstance(result, Path)
