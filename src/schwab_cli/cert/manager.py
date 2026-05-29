"""High-level orchestration of certificate install / uninstall / status.

Ties together :mod:`schwab_cli.cert.generate`, :mod:`schwab_cli.cert.store`
and a :class:`~schwab_cli.cert.keychain.TrustStore` to manage the lifecycle
of the local CA + leaf certificate.

Security: by default the CA private key is *transient* — generated in memory,
used to sign the leaf, and never written to disk (``persist_ca_key=False``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from schwab_cli.cert import generate, store
from schwab_cli.cert.generate import CA_COMMON_NAME, CertKeyPair
from schwab_cli.cert.keychain import TrustStore
from schwab_cli.cert.store import Manifest

LEAF_RENEW_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class LeafPaths:
    """Filesystem paths to the leaf certificate and its private key."""

    cert: Path
    key: Path


@dataclass(frozen=True)
class CertStatus:
    """Snapshot of the installed certificate state."""

    manifest_present: bool
    ca_trusted: bool
    leaf_cert_present: bool
    leaf_key_present: bool
    leaf_valid_until: str | None


class LeafAbsentError(Exception):
    """Raised when the leaf cert/key are needed but not on disk.

    The string form always hints at running ``schwab cert install`` so the
    user knows how to recover, regardless of the constructor message.
    """

    _HINT = "Run `schwab cert install` to generate the leaf certificate."

    def __init__(self, message: str = "") -> None:
        full = f"{message} {self._HINT}".strip() if message else self._HINT
        super().__init__(full)


class CertManager:
    """Orchestrates the local CA + leaf certificate lifecycle."""

    def __init__(
        self, trust_store: TrustStore, store_dir: Path | None = None
    ) -> None:
        self._trust_store = trust_store
        self._dir = store_dir

    # -- internal helpers --------------------------------------------------

    def _paths(self):
        return store.paths(self._dir)

    def _read_manifest(self) -> Manifest | None:
        return store.read_manifest(self._dir)

    # -- install -----------------------------------------------------------

    def install(self, persist_ca_key: bool = False) -> LeafPaths:
        """Install the local CA + leaf certificate (idempotent on trust)."""
        paths = self._paths()
        manifest = self._read_manifest()

        if manifest is not None and paths.ca_cert.exists():
            if self._trust_store.is_trusted(CA_COMMON_NAME):
                # Already installed and trusted — nothing to do.
                return LeafPaths(cert=paths.leaf_cert, key=paths.leaf_key)
            # Files exist but the OS no longer trusts the CA — redo trust.
            self._trust_store.add_trusted_root(paths.ca_cert)
            return LeafPaths(cert=paths.leaf_cert, key=paths.leaf_key)

        # Fresh generation.
        ca = generate.generate_ca()
        leaf = generate.generate_leaf(ca)

        ca_cert_path = store.write_ca_cert(
            generate.cert_to_pem(ca.cert), self._dir
        )
        leaf_cert_path, leaf_key_path = store.write_leaf(
            generate.cert_to_pem(leaf.cert),
            generate.key_to_pem(leaf.key),
            self._dir,
        )
        if persist_ca_key:
            store.write_ca_key(generate.key_to_pem(ca.key), self._dir)

        # Write the manifest BEFORE trusting the root: if add_trusted_root
        # raises, the manifest persists so the next install sees a
        # "present but not trusted" CA and re-runs the trust step (rather
        # than orphaning a trusted CA with no record of it).
        store.write_manifest(self._build_manifest(ca), self._dir)

        self._trust_store.add_trusted_root(ca_cert_path)

        return LeafPaths(cert=leaf_cert_path, key=leaf_key_path)

    @staticmethod
    def _build_manifest(ca: CertKeyPair) -> Manifest:
        return Manifest(
            # Plain-hex SHA-256, the form `security delete-certificate -Z`
            # accepts; a non-cryptographic CLI identifier, not a trust check.
            ca_sha256=generate.sha256_fingerprint_hex(ca.cert),
            ca_cn=CA_COMMON_NAME,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    # -- uninstall ---------------------------------------------------------

    def uninstall(self, by_label: bool = False) -> str:
        """Remove the CA from the trust store and delete on-disk artifacts."""
        paths = self._paths()
        manifest = self._read_manifest()

        if manifest is not None:
            self._trust_store.remove(manifest.ca_sha256)
            self._delete_all(paths)
            return "Uninstalled Schwab CLI Local CA and removed certificate files."

        if by_label:
            self._trust_store.remove_by_label(CA_COMMON_NAME)
            self._delete_all(paths)
            return (
                "No manifest found; attempted removal of "
                f"{CA_COMMON_NAME!r} by label and cleaned up any stray files."
            )

        return "Nothing to remove: no Schwab CLI certificate manifest found."

    @staticmethod
    def _delete_all(paths) -> None:
        for p in (
            paths.leaf_cert,
            paths.leaf_key,
            paths.ca_cert,
            paths.ca_key,
            paths.manifest,
        ):
            p.unlink(missing_ok=True)

    # -- status ------------------------------------------------------------

    def status(self) -> CertStatus:
        """Return a snapshot of the current certificate state."""
        paths = self._paths()
        manifest = self._read_manifest()

        ca_trusted = False
        if manifest is not None:
            ca_trusted = bool(self._trust_store.is_trusted(CA_COMMON_NAME))

        return CertStatus(
            manifest_present=manifest is not None,
            ca_trusted=ca_trusted,
            leaf_cert_present=paths.leaf_cert.exists(),
            leaf_key_present=paths.leaf_key.exists(),
            leaf_valid_until=self._leaf_valid_until(paths.leaf_cert),
        )

    @staticmethod
    def _leaf_valid_until(leaf_cert: Path) -> str | None:
        if not leaf_cert.exists():
            return None
        try:
            cert = x509.load_pem_x509_certificate(leaf_cert.read_bytes())
            return cert.not_valid_after_utc.isoformat()
        except Exception:  # noqa: BLE001 - unreadable cert -> None
            return None

    # -- leaf access -------------------------------------------------------

    def leaf_paths(self) -> LeafPaths:
        """Return leaf paths, raising LeafAbsentError when files are missing."""
        paths = self._paths()
        if not paths.leaf_cert.exists() or not paths.leaf_key.exists():
            raise LeafAbsentError(
                "Leaf certificate not found. Run `schwab cert install` first."
            )
        return LeafPaths(cert=paths.leaf_cert, key=paths.leaf_key)

    def ensure_leaf(self) -> LeafPaths | None:
        """Ensure a valid leaf exists, regenerating from a persisted CA key.

        Returns None when the CA key is not persisted (transient-key default),
        signalling the caller to prompt for a re-install.
        """
        paths = self._paths()

        if not paths.ca_key.exists():
            return None

        if self._leaf_is_current(paths.leaf_cert):
            return LeafPaths(cert=paths.leaf_cert, key=paths.leaf_key)

        ca = self._load_ca(paths)
        leaf = generate.generate_leaf(ca)
        leaf_cert_path, leaf_key_path = store.write_leaf(
            generate.cert_to_pem(leaf.cert),
            generate.key_to_pem(leaf.key),
            self._dir,
        )
        return LeafPaths(cert=leaf_cert_path, key=leaf_key_path)

    @staticmethod
    def _leaf_is_current(leaf_cert: Path) -> bool:
        if not leaf_cert.exists():
            return False
        try:
            cert = x509.load_pem_x509_certificate(leaf_cert.read_bytes())
        except Exception:  # noqa: BLE001
            return False
        remaining = cert.not_valid_after_utc - datetime.now(timezone.utc)
        return remaining > LEAF_RENEW_WINDOW

    def _load_ca(self, paths) -> CertKeyPair:
        ca_key = load_pem_private_key(paths.ca_key.read_bytes(), password=None)
        if not isinstance(ca_key, rsa.RSAPrivateKey):
            raise ValueError(
                "Persisted CA key is not an RSA private key. "
                "Re-run `schwab cert install` to regenerate it."
            )
        ca_cert = x509.load_pem_x509_certificate(paths.ca_cert.read_bytes())
        return CertKeyPair(cert=ca_cert, key=ca_key)
