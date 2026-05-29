"""On-disk layout and (de)serialisation for certificate artifacts.

All artifacts live under ``config_dir() / "certs"`` (honouring
``SCHWAB_CLI_CONFIG_DIR``). Every writer chmods the file to 0600 and the
parent directory to 0700. The CA private key is only written when a caller
opts in via :func:`write_ca_key`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from schwab_cli.paths import config_dir

CERT_DIR_NAME = "certs"
CA_CERT_NAME = "ca.pem"
CA_KEY_NAME = "ca-key.pem"
LEAF_CERT_NAME = "127.0.0.1.pem"
LEAF_KEY_NAME = "127.0.0.1-key.pem"
MANIFEST_NAME = "manifest.json"

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_RECOVERY_HINT = (
    "Run `schwab cert uninstall` then `schwab cert install` to regenerate it."
)


class ManifestCorruptError(Exception):
    """Raised when manifest.json exists but cannot be parsed.

    Distinct from the genuinely-absent case (which returns ``None``). The
    message always includes a recovery hint so the user knows how to recover.
    """

    def __init__(self, message: str = "") -> None:
        base = message or "Certificate manifest is corrupt."
        super().__init__(f"{base} {_RECOVERY_HINT}".strip())


@dataclass(frozen=True)
class Manifest:
    """Metadata describing the installed CA."""

    ca_sha256: str
    ca_cn: str
    created_at: str


@dataclass(frozen=True)
class CertPaths:
    """Resolved filesystem paths for all certificate artifacts."""

    ca_cert: Path
    leaf_cert: Path
    leaf_key: Path
    manifest: Path
    ca_key: Path


def cert_dir() -> Path:
    """Return ``config_dir() / "certs"``."""
    return config_dir() / CERT_DIR_NAME


def _resolve_dir(base_dir: Path | None) -> Path:
    return base_dir if base_dir is not None else cert_dir()


def paths(base_dir: Path | None = None) -> CertPaths:
    """Return resolved paths for all certificate artifacts."""
    base = _resolve_dir(base_dir)
    return CertPaths(
        ca_cert=base / CA_CERT_NAME,
        leaf_cert=base / LEAF_CERT_NAME,
        leaf_key=base / LEAF_KEY_NAME,
        manifest=base / MANIFEST_NAME,
        ca_key=base / CA_KEY_NAME,
    )


def _ensure_dir(base: Path) -> None:
    # Pass mode to mkdir so the dir is never momentarily world-accessible;
    # umask can mask the mkdir mode, so chmod afterwards to guarantee 0700.
    base.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    base.chmod(_DIR_MODE)


def _write_secure(path: Path, data: bytes) -> Path:
    _ensure_dir(path.parent)
    # Create with 0600 atomically (O_CREAT honours the mode only on creation),
    # avoiding the TOCTOU window of write-then-chmod where the file briefly
    # exists with the umask-derived (possibly group/world-readable) mode.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # An O_CREAT mode is ignored when the file already exists; enforce 0600 on
    # overwrite too.
    path.chmod(_FILE_MODE)
    return path


def write_ca_cert(pem: bytes, base_dir: Path | None = None) -> Path:
    """Write ca.pem (0600), creating the cert dir (0700) if absent."""
    return _write_secure(paths(base_dir).ca_cert, pem)


def write_leaf(
    cert_pem: bytes, key_pem: bytes, base_dir: Path | None = None
) -> tuple[Path, Path]:
    """Write the leaf cert and key (both 0600). Never writes the CA key."""
    p = paths(base_dir)
    cert_path = _write_secure(p.leaf_cert, cert_pem)
    key_path = _write_secure(p.leaf_key, key_pem)
    return cert_path, key_path


def write_ca_key(pem: bytes, base_dir: Path | None = None) -> Path:
    """Write ca-key.pem (0600). Only used when persist_ca_key=True."""
    return _write_secure(paths(base_dir).ca_key, pem)


def write_manifest(manifest: Manifest, base_dir: Path | None = None) -> Path:
    """Serialise the manifest to manifest.json (0600)."""
    data = json.dumps(asdict(manifest), indent=2).encode("utf-8")
    return _write_secure(paths(base_dir).manifest, data)


def read_manifest(base_dir: Path | None = None) -> Manifest | None:
    """Read manifest.json.

    Returns ``None`` only when the file is genuinely absent. Raises
    :class:`ManifestCorruptError` when the file exists but is malformed JSON
    or is missing required keys.
    """
    manifest_path = paths(base_dir).manifest
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text())
        return Manifest(
            ca_sha256=raw["ca_sha256"],
            ca_cn=raw["ca_cn"],
            created_at=raw["created_at"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ManifestCorruptError(
            f"Failed to read {manifest_path}: {exc}."
        ) from exc


def clear_manifest(base_dir: Path | None = None) -> None:
    """Delete manifest.json if present; no-op otherwise."""
    manifest_path = paths(base_dir).manifest
    manifest_path.unlink(missing_ok=True)
