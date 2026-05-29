"""Unit tests for schwab_cli.cert.store (RED phase — no implementation yet).

API CONTRACT settled by these tests
====================================
Module: ``schwab_cli.cert.store``

``cert_dir() -> Path``
    Returns ``config_dir() / "certs"``.
    Respects ``SCHWAB_CLI_CONFIG_DIR`` env var via ``paths.config_dir()``.

``paths() -> CertPaths``
    Returns a dataclass/namedtuple with attributes:
        ca_cert  : Path  — <cert_dir>/ca.pem
        leaf_cert: Path  — <cert_dir>/127.0.0.1.pem
        leaf_key : Path  — <cert_dir>/127.0.0.1-key.pem
        manifest : Path  — <cert_dir>/manifest.json
        ca_key   : Path  — <cert_dir>/ca-key.pem
                           (present in paths() but NOT written by default)

``write_ca_cert(pem: bytes, dir: Path | None = None) -> Path``
    Writes ca.pem into cert_dir (or given dir), chmod 0600.
    Creates cert_dir with chmod 0700 if not present.

``write_leaf(cert_pem: bytes, key_pem: bytes, dir: Path | None = None) -> tuple[Path, Path]``
    Writes 127.0.0.1.pem (0600) and 127.0.0.1-key.pem (0600).
    Creates cert_dir (0700) if absent.

``write_ca_key(pem: bytes, dir: Path | None = None) -> Path``
    Writes ca-key.pem (0600). Used only when persist_ca_key=True.

``Manifest``
    Frozen dataclass (value object) with fields:
        ca_sha256  : str   (plain-hex SHA-256, no colons; CLI identifier)
        ca_cn      : str
        created_at : str   (ISO-8601 string, e.g. datetime.utcnow().isoformat())

``write_manifest(manifest: Manifest, dir: Path | None = None) -> Path``
    Serialises to JSON at manifest.json; file mode 0600.

``read_manifest(dir: Path | None = None) -> Manifest | None``
    Deserialises from manifest.json; returns None when file is absent.

``clear_manifest(dir: Path | None = None) -> None``
    Deletes manifest.json if present; no-op if absent.

ALL tests MUST isolate via SCHWAB_CLI_CONFIG_DIR (monkeypatch + tmp_path).
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from schwab_cli.cert.store import (
    Manifest,
    ManifestCorruptError,
    cert_dir,
    clear_manifest,
    paths,
    read_manifest,
    write_ca_cert,
    write_ca_key,
    write_leaf,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture(autouse=True)
def _isolate_config_dir(monkeypatch, tmp_path):
    """Redirect config_dir() to tmp_path for every test in this file."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# cert_dir()
# ---------------------------------------------------------------------------


def test_cert_dir_is_under_config_dir(tmp_path):
    d = cert_dir()
    assert d == tmp_path / "certs"


def test_cert_dir_returns_path_type(tmp_path):
    assert isinstance(cert_dir(), Path)


# ---------------------------------------------------------------------------
# paths()
# ---------------------------------------------------------------------------


def test_paths_ca_cert_filename(tmp_path):
    p = paths()
    assert p.ca_cert.name == "ca.pem"
    assert p.ca_cert.parent == tmp_path / "certs"


def test_paths_leaf_cert_filename(tmp_path):
    p = paths()
    assert p.leaf_cert.name == "127.0.0.1.pem"


def test_paths_leaf_key_filename(tmp_path):
    p = paths()
    assert p.leaf_key.name == "127.0.0.1-key.pem"


def test_paths_manifest_filename(tmp_path):
    p = paths()
    assert p.manifest.name == "manifest.json"


def test_paths_ca_key_filename(tmp_path):
    """ca-key path is exposed even though not written by default."""
    p = paths()
    assert p.ca_key.name == "ca-key.pem"


def test_paths_all_under_cert_dir(tmp_path):
    p = paths()
    cdir = tmp_path / "certs"
    assert p.ca_cert.parent == cdir
    assert p.leaf_cert.parent == cdir
    assert p.leaf_key.parent == cdir
    assert p.manifest.parent == cdir
    assert p.ca_key.parent == cdir


# ---------------------------------------------------------------------------
# write_ca_cert — creates dir + file with correct modes
# ---------------------------------------------------------------------------


def test_write_ca_cert_creates_cert_dir(tmp_path):
    write_ca_cert(b"FAKE_PEM_CERT")
    assert (tmp_path / "certs").is_dir()


def test_write_ca_cert_dir_mode_is_0700(tmp_path):
    write_ca_cert(b"FAKE_PEM_CERT")
    assert _mode(tmp_path / "certs") == 0o700


def test_write_ca_cert_file_mode_is_0600(tmp_path):
    write_ca_cert(b"FAKE_PEM_CERT")
    assert _mode(tmp_path / "certs" / "ca.pem") == 0o600


def test_write_ca_cert_writes_given_bytes(tmp_path):
    content = b"-----BEGIN CERTIFICATE-----\nXXXX\n-----END CERTIFICATE-----\n"
    write_ca_cert(content)
    assert (tmp_path / "certs" / "ca.pem").read_bytes() == content


def test_write_ca_cert_returns_path(tmp_path):
    result = write_ca_cert(b"FAKE")
    assert isinstance(result, Path)
    assert result == tmp_path / "certs" / "ca.pem"


def test_write_ca_cert_idempotent_overwrites(tmp_path):
    write_ca_cert(b"FIRST")
    write_ca_cert(b"SECOND")
    assert (tmp_path / "certs" / "ca.pem").read_bytes() == b"SECOND"


# ---------------------------------------------------------------------------
# write_leaf — cert + key, modes
# ---------------------------------------------------------------------------


def test_write_leaf_creates_cert_file(tmp_path):
    write_leaf(b"CERT_PEM", b"KEY_PEM")
    assert (tmp_path / "certs" / "127.0.0.1.pem").exists()


def test_write_leaf_creates_key_file(tmp_path):
    write_leaf(b"CERT_PEM", b"KEY_PEM")
    assert (tmp_path / "certs" / "127.0.0.1-key.pem").exists()


def test_write_leaf_cert_mode_is_0600(tmp_path):
    write_leaf(b"CERT_PEM", b"KEY_PEM")
    assert _mode(tmp_path / "certs" / "127.0.0.1.pem") == 0o600


def test_write_leaf_key_mode_is_0600(tmp_path):
    write_leaf(b"CERT_PEM", b"KEY_PEM")
    assert _mode(tmp_path / "certs" / "127.0.0.1-key.pem") == 0o600


def test_write_leaf_cert_dir_mode_is_0700(tmp_path):
    write_leaf(b"CERT_PEM", b"KEY_PEM")
    assert _mode(tmp_path / "certs") == 0o700


def test_write_leaf_returns_tuple_of_cert_and_key_paths(tmp_path):
    result = write_leaf(b"CERT_PEM", b"KEY_PEM")
    cert_path, key_path = result
    assert cert_path == tmp_path / "certs" / "127.0.0.1.pem"
    assert key_path == tmp_path / "certs" / "127.0.0.1-key.pem"


def test_write_leaf_writes_correct_cert_bytes(tmp_path):
    cert_pem = b"CERT_BYTES"
    write_leaf(cert_pem, b"KEY")
    assert (tmp_path / "certs" / "127.0.0.1.pem").read_bytes() == cert_pem


def test_write_leaf_writes_correct_key_bytes(tmp_path):
    key_pem = b"KEY_BYTES"
    write_leaf(b"CERT", key_pem)
    assert (tmp_path / "certs" / "127.0.0.1-key.pem").read_bytes() == key_pem


# ---------------------------------------------------------------------------
# write_ca_key (opt-in persist)
# ---------------------------------------------------------------------------


def test_write_ca_key_creates_file(tmp_path):
    write_ca_key(b"CA_KEY_PEM")
    assert (tmp_path / "certs" / "ca-key.pem").exists()


def test_write_ca_key_mode_is_0600(tmp_path):
    write_ca_key(b"CA_KEY_PEM")
    assert _mode(tmp_path / "certs" / "ca-key.pem") == 0o600


def test_write_ca_key_returns_path(tmp_path):
    result = write_ca_key(b"CA_KEY_PEM")
    assert result == tmp_path / "certs" / "ca-key.pem"


def test_write_leaf_does_NOT_write_ca_key(tmp_path):
    """Transient-key default: write_leaf must never create ca-key.pem."""
    write_leaf(b"CERT", b"KEY")
    assert not (tmp_path / "certs" / "ca-key.pem").exists()


def test_write_ca_cert_does_NOT_write_ca_key(tmp_path):
    write_ca_cert(b"CA_CERT")
    assert not (tmp_path / "certs" / "ca-key.pem").exists()


# ---------------------------------------------------------------------------
# Manifest dataclass
# ---------------------------------------------------------------------------


def test_manifest_has_required_fields():
    m = Manifest(
        ca_sha256="AABB",
        ca_cn="Schwab CLI Local CA",
        created_at="2025-01-01T00:00:00",
    )
    assert m.ca_sha256 == "AABB"
    assert m.ca_cn == "Schwab CLI Local CA"
    assert m.created_at == "2025-01-01T00:00:00"


def test_manifest_is_frozen():
    m = Manifest(ca_sha256="AABB", ca_cn="CN", created_at="2025-01-01")
    with pytest.raises(Exception):
        m.ca_sha256 = "CCDD"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# write_manifest / read_manifest round-trip
# ---------------------------------------------------------------------------


def test_write_manifest_creates_file(tmp_path):
    m = Manifest(ca_sha256="A", ca_cn="CN", created_at="2025-01-01")
    write_manifest(m)
    assert (tmp_path / "certs" / "manifest.json").exists()


def test_write_manifest_mode_is_0600(tmp_path):
    m = Manifest(ca_sha256="A", ca_cn="CN", created_at="2025-01-01")
    write_manifest(m)
    assert _mode(tmp_path / "certs" / "manifest.json") == 0o600


def test_write_manifest_writes_valid_json(tmp_path):
    m = Manifest(ca_sha256="A", ca_cn="CN", created_at="2025-01-01")
    write_manifest(m)
    raw = json.loads((tmp_path / "certs" / "manifest.json").read_text())
    assert isinstance(raw, dict)


def test_read_manifest_returns_none_when_absent(tmp_path):
    result = read_manifest()
    assert result is None


def test_read_manifest_round_trips_all_fields(tmp_path):
    m = Manifest(
        ca_sha256="ABCDEF",
        ca_cn="Schwab CLI Local CA",
        created_at="2025-06-01T12:00:00",
    )
    write_manifest(m)
    loaded = read_manifest()
    assert loaded is not None
    assert loaded.ca_sha256 == m.ca_sha256
    assert loaded.ca_cn == m.ca_cn
    assert loaded.created_at == m.created_at


def test_read_manifest_returns_manifest_instance(tmp_path):
    m = Manifest(ca_sha256="X", ca_cn="Z", created_at="T")
    write_manifest(m)
    loaded = read_manifest()
    assert isinstance(loaded, Manifest)


# ---------------------------------------------------------------------------
# clear_manifest
# ---------------------------------------------------------------------------


def test_clear_manifest_deletes_file(tmp_path):
    m = Manifest(ca_sha256="A", ca_cn="C", created_at="D")
    write_manifest(m)
    clear_manifest()
    assert not (tmp_path / "certs" / "manifest.json").exists()


def test_clear_manifest_is_noop_when_absent(tmp_path):
    # Should not raise even if manifest.json does not exist.
    clear_manifest()  # no exception


def test_read_manifest_returns_none_after_clear(tmp_path):
    m = Manifest(ca_sha256="A", ca_cn="C", created_at="D")
    write_manifest(m)
    clear_manifest()
    assert read_manifest() is None


# ---------------------------------------------------------------------------
# read_manifest — corrupt file handling
# ---------------------------------------------------------------------------


def _write_raw_manifest(tmp_path: Path, text: str) -> None:
    cdir = tmp_path / "certs"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "manifest.json").write_text(text)


def test_read_manifest_raises_on_malformed_json(tmp_path):
    _write_raw_manifest(tmp_path, "{not valid json")
    with pytest.raises(ManifestCorruptError):
        read_manifest()


def test_read_manifest_raises_on_missing_keys(tmp_path):
    _write_raw_manifest(tmp_path, json.dumps({"ca_sha256": "A"}))
    with pytest.raises(ManifestCorruptError):
        read_manifest()


def test_manifest_corrupt_error_includes_recovery_hint(tmp_path):
    _write_raw_manifest(tmp_path, "[]")
    with pytest.raises(ManifestCorruptError) as exc_info:
        read_manifest()
    msg = str(exc_info.value).lower()
    assert "uninstall" in msg or "install" in msg


def test_manifest_corrupt_error_is_exception_subclass():
    assert issubclass(ManifestCorruptError, Exception)


# ---------------------------------------------------------------------------
# SCHWAB_CLI_CONFIG_DIR isolation guard
# ---------------------------------------------------------------------------


def test_cert_dir_does_not_touch_real_home(tmp_path):
    """cert_dir() must point to tmp_path/certs, never the real ~/.config."""
    d = cert_dir()
    home = Path.home()
    assert not str(d).startswith(str(home / ".config" / "schwab_cli")), (
        f"cert_dir() resolved to {d} which is inside real home config — isolation broken"
    )
