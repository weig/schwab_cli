import json
import os
import stat

import pytest

from schwab_cli.session import Session, SessionError, load, save, session_path


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _write(tmp_path, data):
    d = tmp_path / ".config" / "schwab_cli"
    d.mkdir(parents=True)
    f = d / "session.json"
    f.write_text(json.dumps(data))
    return f


def test_session_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert session_path() == tmp_path / ".config" / "schwab_cli" / "session.json"


def test_session_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert session_path() == tmp_path / "xdg" / "schwab_cli" / "session.json"


def test_session_path_honors_schwab_cli_config_dir(monkeypatch, tmp_path):
    """SCHWAB_CLI_CONFIG_DIR points directly at the schwab_cli dir
    (no ``schwab_cli`` suffix), and the session file sits in that dir."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path / "isolated"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # ignored
    assert session_path() == tmp_path / "isolated" / "session.json"


def test_session_save_load_roundtrip_with_config_dir(monkeypatch, tmp_path):
    """End-to-end: writing then reading through SCHWAB_CLI_CONFIG_DIR
    keeps everything inside the override directory."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path / "iso"))
    s = Session(
        access_token="a", refresh_token="r",
        expires_at=100, refresh_token_expires_at=200,
    )
    save(s)
    assert (tmp_path / "iso" / "session.json").exists()
    # No file written under the real home / xdg location.
    assert not (tmp_path / "xdg" / "schwab_cli" / "session.json").exists()
    assert load() == s


def test_session_is_frozen():
    s = Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2)
    with pytest.raises(Exception):
        s.access_token = "x"  # type: ignore[misc]


def test_load_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert load() is None


def test_load_parses_full_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 1,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 100,
        "refresh_token_expires_at": 200,
    })
    assert load() == Session(
        access_token="a", refresh_token="r", expires_at=100, refresh_token_expires_at=200
    )


def test_load_raises_on_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    d = tmp_path / ".config" / "schwab_cli"
    d.mkdir(parents=True)
    (d / "session.json").write_text("{not valid")
    with pytest.raises(SessionError, match="malformed"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 1,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 100,
    })  # missing refresh_token_expires_at
    with pytest.raises(SessionError, match="refresh_token_expires_at"):
        load()


def test_load_raises_on_unsupported_version(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 999,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 1,
        "refresh_token_expires_at": 2,
    })
    with pytest.raises(SessionError, match="version"):
        load()


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2))
    f = tmp_path / ".config" / "schwab_cli" / "session.json"
    assert f.exists()
    assert _mode(f) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_through_load(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    original = Session(access_token="a", refresh_token="r", expires_at=100, refresh_token_expires_at=200)
    save(original)
    assert load() == original


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="orig_a", refresh_token="orig_r", expires_at=1, refresh_token_expires_at=2))
    f = tmp_path / ".config" / "schwab_cli" / "session.json"
    original_bytes = f.read_bytes()

    def boom(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Session(access_token="new_a", refresh_token="new_r", expires_at=10, refresh_token_expires_at=20))
    assert f.read_bytes() == original_bytes
    assert list((tmp_path / ".config" / "schwab_cli").glob("*.tmp")) == []


def test_from_token_response_computes_expiries():
    from schwab_cli.oauth import TokenResponse
    tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1800)
    s = Session.from_token_response(tr, now=1_000_000)
    assert s.access_token == "a"
    assert s.refresh_token == "r"
    assert s.expires_at == 1_000_000 + 1800
    assert s.refresh_token_expires_at == 1_000_000 + 7 * 24 * 3600
