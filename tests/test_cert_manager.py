"""Unit tests for schwab_cli.cert.manager (RED phase — no implementation yet).

API CONTRACT settled by these tests
====================================
Module: ``schwab_cli.cert.manager``

``CertManager``
    Orchestration class. Constructor:
        CertManager(trust_store: TrustStore, store_dir: Path | None = None)
    When store_dir is None, uses cert_dir() (respects SCHWAB_CLI_CONFIG_DIR).
    When store_dir is provided, all cert.store operations use that directory.

``LeafPaths``
    Dataclass/namedtuple with fields:
        cert : Path
        key  : Path

``LeafAbsentError``
    Exception raised by leaf_paths() when the leaf cert/key are not present.
    Must be a subclass of Exception and carry a message hinting "run `schwab cert install`".

``install(persist_ca_key: bool = False) -> LeafPaths``
    Idempotent installation:
    1. Generates CA + leaf (always fresh key pair).
    2. Calls trust_store.add_trusted_root(ca_cert_path).
    3. Writes CA cert, leaf cert, leaf key (NOT CA key by default).
    4. Writes manifest (ca_sha256 [plain-hex], ca_cn, created_at) BEFORE the
       trust step, so a trust failure leaves a recoverable record.
    5. Returns LeafPaths(cert=<leaf_cert_path>, key=<leaf_key_path>).
    Idempotency rule: if manifest exists AND trust_store.is_trusted(CA_COMMON_NAME)
    returns True, skip regeneration and trust step and return existing leaf paths.
    If manifest exists but is_trusted returns False, redo trust step (and optionally
    regenerate). Trust step is NEVER skipped just because files exist on disk.

``uninstall(by_label: bool = False) -> str``
    Returns a human-readable status string.
    When manifest exists:
        Calls trust_store.remove(manifest.ca_sha256).
        Deletes CA cert, leaf cert, leaf key, manifest.json.
        Does NOT delete ca-key.pem if it doesn't exist.
        Returns a "uninstalled" message.
    When manifest is absent and by_label=False:
        Does NOT call trust_store.remove().
        Returns a "nothing to remove" / no-op message.
    When manifest is absent and by_label=True:
        Calls trust_store.remove with a known-label / CN-based lookup
        (exact argv format defined in keychain tests; manager just calls
         trust_store.remove_by_label(cn) or similar — see deviation note below).
        Deviation: ``uninstall(by_label=True)`` calls
        ``trust_store.remove_by_label("Schwab CLI Local CA")`` on the TrustStore.
        The fake TrustStore in these tests must therefore also expose remove_by_label.

``status() -> CertStatus``
    Dataclass with fields:
        manifest_present   : bool
        ca_trusted         : bool   (False when manifest absent)
        leaf_cert_present  : bool
        leaf_key_present   : bool
        leaf_valid_until   : str | None  (ISO-8601 or None when absent/unreadable)

``leaf_paths() -> LeafPaths``
    Returns LeafPaths(cert, key).
    Raises LeafAbsentError when either file is missing.

``ensure_leaf() -> LeafPaths | None``
    If CA key is persisted (ca-key.pem exists), regen leaf if missing or expiring
    within 30 days; returns LeafPaths.
    If CA key is absent, returns None and does NOT raise (caller should prompt
    "re-run cert install").

ALL tests MUST isolate via SCHWAB_CLI_CONFIG_DIR (monkeypatch + tmp_path) and
inject a fake TrustStore — no real security binary is ever called.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pytest

from schwab_cli.cert.manager import CertManager, CertStatus, LeafAbsentError, LeafPaths


# ---------------------------------------------------------------------------
# Fake TrustStore
# ---------------------------------------------------------------------------


@dataclass
class _FakeTrustStoreState:
    """Mutable state bag shared between calls so tests can inspect behaviour."""

    add_calls: list[str]
    remove_calls: list[str]
    remove_by_label_calls: list[str]
    is_trusted_result: bool
    remove_raises: Exception | None = None
    add_raises: Exception | None = None


class FakeTrustStore:
    """Injectable fake implementing the TrustStore Protocol."""

    def __init__(self, *, is_trusted_result: bool = True):
        self.state = _FakeTrustStoreState(
            add_calls=[],
            remove_calls=[],
            remove_by_label_calls=[],
            is_trusted_result=is_trusted_result,
        )

    def add_trusted_root(self, pem_path) -> None:
        if self.state.add_raises:
            raise self.state.add_raises
        self.state.add_calls.append(str(pem_path))

    def is_trusted(self, cn: str) -> bool:
        return self.state.is_trusted_result

    def remove(self, sha256: str) -> None:
        if self.state.remove_raises:
            raise self.state.remove_raises
        self.state.remove_calls.append(sha256)

    def remove_by_label(self, cn: str) -> None:
        self.state.remove_by_label_calls.append(cn)


@pytest.fixture(autouse=True)
def _isolate_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))


def _make_manager(is_trusted: bool = True, tmp_path: Path | None = None):
    trust_store = FakeTrustStore(is_trusted_result=is_trusted)
    return CertManager(trust_store=trust_store, store_dir=tmp_path), trust_store


# ---------------------------------------------------------------------------
# install — happy path
# ---------------------------------------------------------------------------


def test_install_returns_leaf_paths(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    result = manager.install()
    assert isinstance(result, LeafPaths)
    assert hasattr(result, "cert")
    assert hasattr(result, "key")


def test_install_leaf_cert_path_exists(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    result = manager.install()
    assert result.cert.exists(), f"Leaf cert not created at {result.cert}"


def test_install_leaf_key_path_exists(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    result = manager.install()
    assert result.key.exists(), f"Leaf key not created at {result.key}"


def test_install_ca_cert_is_written(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    ca_cert = tmp_path / "ca.pem"
    assert ca_cert.exists(), "CA cert must be written during install"


def test_install_calls_trust_store_add_trusted_root_once(tmp_path):
    manager, trust_store = _make_manager(tmp_path=tmp_path)
    manager.install()
    assert len(trust_store.state.add_calls) == 1


def test_install_passes_ca_cert_path_to_trust_store(tmp_path):
    manager, trust_store = _make_manager(tmp_path=tmp_path)
    result = manager.install()
    # The path passed to add_trusted_root should point to ca.pem
    added_path = trust_store.state.add_calls[0]
    assert "ca.pem" in added_path


def test_install_writes_manifest(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    from schwab_cli.cert.store import read_manifest

    m = read_manifest(tmp_path)
    assert m is not None


def test_install_manifest_contains_ca_sha256(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    from schwab_cli.cert.store import read_manifest

    m = read_manifest(tmp_path)
    assert m.ca_sha256 and len(m.ca_sha256) > 0


def test_install_manifest_ca_sha256_is_plain_hex_no_colons(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    from schwab_cli.cert.store import read_manifest

    m = read_manifest(tmp_path)
    # SHA-256 = 64 hex chars, no colons (the form `security -Z` accepts).
    assert ":" not in m.ca_sha256
    assert len(m.ca_sha256) == 64
    assert m.ca_sha256 == m.ca_sha256.upper()


def test_install_manifest_ca_cn_is_schwab_cli_local_ca(tmp_path):
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    from schwab_cli.cert.store import read_manifest

    m = read_manifest(tmp_path)
    assert m.ca_cn == "Schwab CLI Local CA"


# ---------------------------------------------------------------------------
# install — transient CA key (SECURITY: §2.1)
# ---------------------------------------------------------------------------


def test_install_default_does_not_write_ca_key(tmp_path):
    """persist_ca_key=False (default): CA private key must NOT be written."""
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install()
    ca_key = tmp_path / "ca-key.pem"
    assert not ca_key.exists(), (
        "CA private key was written with persist_ca_key=False — transient-key security violation"
    )


def test_install_persist_ca_key_true_writes_ca_key(tmp_path):
    """persist_ca_key=True must persist the CA private key."""
    manager, _ = _make_manager(tmp_path=tmp_path)
    manager.install(persist_ca_key=True)
    ca_key = tmp_path / "ca-key.pem"
    assert ca_key.exists(), "CA private key should be written when persist_ca_key=True"


def test_install_persist_ca_key_false_writes_leaf_key(tmp_path):
    """The leaf private key IS always written (regardless of persist_ca_key)."""
    manager, _ = _make_manager(tmp_path=tmp_path)
    result = manager.install(persist_ca_key=False)
    assert result.key.exists()


# ---------------------------------------------------------------------------
# install — idempotency (trust-based)
# ---------------------------------------------------------------------------


def test_install_idempotent_when_already_trusted_skips_add(tmp_path):
    """If manifest exists and is_trusted returns True, add_trusted_root is not called again."""
    manager, trust_store = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    first_add_count = len(trust_store.state.add_calls)
    # Second install — already trusted
    manager.install()
    assert len(trust_store.state.add_calls) == first_add_count, (
        "add_trusted_root should not be called again when CA is already trusted"
    )


def test_install_redoes_trust_when_not_trusted(tmp_path):
    """If manifest exists but is_trusted returns False, add_trusted_root IS called again."""
    # First install with trusted=True to write manifest
    manager_trusted, ts_trusted = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager_trusted.install()
    assert len(ts_trusted.state.add_calls) == 1

    # Second install with a fresh manager where is_trusted=False
    trust_store_untrusted = FakeTrustStore(is_trusted_result=False)
    manager2 = CertManager(trust_store=trust_store_untrusted, store_dir=tmp_path)
    manager2.install()
    assert len(trust_store_untrusted.state.add_calls) >= 1, (
        "install must redo trust step when is_trusted returns False"
    )


def test_install_second_call_returns_same_leaf_cert_path_when_trusted(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    first = manager.install()
    second = manager.install()
    assert first.cert == second.cert
    assert first.key == second.key


# ---------------------------------------------------------------------------
# install — ordering: manifest persisted before trust (no orphaned CA)
# ---------------------------------------------------------------------------


def test_install_writes_manifest_before_trusting_root(tmp_path):
    """The manifest must be on disk by the time add_trusted_root runs, so a
    failure there leaves a recoverable 'present but not trusted' state."""
    from schwab_cli.cert.store import read_manifest

    trust_store = FakeTrustStore(is_trusted_result=True)
    seen = {}

    original_add = trust_store.add_trusted_root

    def spy(pem_path):
        # Manifest must already exist when trust is attempted.
        seen["manifest_at_trust"] = read_manifest(tmp_path)
        return original_add(pem_path)

    trust_store.add_trusted_root = spy  # type: ignore[method-assign]
    manager = CertManager(trust_store=trust_store, store_dir=tmp_path)
    manager.install()

    assert seen["manifest_at_trust"] is not None, (
        "manifest must be written before add_trusted_root is called"
    )


def test_install_manifest_persists_when_trust_step_fails(tmp_path):
    """If add_trusted_root raises, the manifest must still be on disk so the
    next install re-runs the trust step (idempotency tied to actual trust)."""
    from schwab_cli.cert.store import read_manifest

    trust_store = FakeTrustStore(is_trusted_result=False)
    trust_store.state.add_raises = RuntimeError("sudo failed")
    manager = CertManager(trust_store=trust_store, store_dir=tmp_path)

    with pytest.raises(RuntimeError):
        manager.install()

    # Manifest persisted despite the trust failure.
    assert read_manifest(tmp_path) is not None

    # Next install (now succeeding) sees present-but-untrusted and re-trusts.
    trust_store2 = FakeTrustStore(is_trusted_result=False)
    manager2 = CertManager(trust_store=trust_store2, store_dir=tmp_path)
    manager2.install()
    assert len(trust_store2.state.add_calls) >= 1


# ---------------------------------------------------------------------------
# uninstall — with manifest
# ---------------------------------------------------------------------------


def test_uninstall_calls_remove_with_manifest_sha256(tmp_path):
    manager, trust_store = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    from schwab_cli.cert.store import read_manifest

    m = read_manifest(tmp_path)
    manager.uninstall()
    assert m.ca_sha256 in trust_store.state.remove_calls


def test_uninstall_deletes_manifest(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    manager.uninstall()
    from schwab_cli.cert.store import read_manifest

    assert read_manifest(tmp_path) is None


def test_uninstall_deletes_leaf_cert(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    result = manager.install()
    manager.uninstall()
    assert not result.cert.exists()


def test_uninstall_deletes_leaf_key(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    result = manager.install()
    manager.uninstall()
    assert not result.key.exists()


def test_uninstall_deletes_ca_cert(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    manager.uninstall()
    assert not (tmp_path / "ca.pem").exists()


def test_uninstall_returns_string(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    result = manager.uninstall()
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# uninstall — no manifest (no-op)
# ---------------------------------------------------------------------------


def test_uninstall_noop_when_no_manifest_does_not_call_remove(tmp_path):
    """When no manifest exists, remove must NOT be called."""
    manager, trust_store = _make_manager(is_trusted=False, tmp_path=tmp_path)
    manager.uninstall()
    assert trust_store.state.remove_calls == [], (
        "remove() must not be called when there is no manifest"
    )


def test_uninstall_noop_returns_nothing_to_remove_message(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    msg = manager.uninstall()
    assert isinstance(msg, str)
    assert len(msg) > 0, "Expected a non-empty 'nothing to remove' message"


# ---------------------------------------------------------------------------
# uninstall — by_label fallback (manifest absent)
# ---------------------------------------------------------------------------


def test_uninstall_by_label_calls_remove_by_label_with_known_cn(tmp_path):
    """by_label=True with no manifest calls remove_by_label("Schwab CLI Local CA")."""
    manager, trust_store = _make_manager(is_trusted=False, tmp_path=tmp_path)
    manager.uninstall(by_label=True)
    assert "Schwab CLI Local CA" in trust_store.state.remove_by_label_calls


def test_uninstall_by_label_false_does_not_call_remove_by_label(tmp_path):
    manager, trust_store = _make_manager(is_trusted=False, tmp_path=tmp_path)
    manager.uninstall(by_label=False)
    assert trust_store.state.remove_by_label_calls == []


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_returns_cert_status_instance(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    result = manager.status()
    assert isinstance(result, CertStatus)


def test_status_has_required_fields(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    s = manager.status()
    assert hasattr(s, "manifest_present")
    assert hasattr(s, "ca_trusted")
    assert hasattr(s, "leaf_cert_present")
    assert hasattr(s, "leaf_key_present")
    assert hasattr(s, "leaf_valid_until")


def test_status_manifest_absent_before_install(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    s = manager.status()
    assert s.manifest_present is False


def test_status_leaf_absent_before_install(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    s = manager.status()
    assert s.leaf_cert_present is False
    assert s.leaf_key_present is False


def test_status_ca_not_trusted_before_install(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    s = manager.status()
    assert s.ca_trusted is False


def test_status_leaf_valid_until_none_before_install(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    s = manager.status()
    assert s.leaf_valid_until is None


def test_status_manifest_present_after_install(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    s = manager.status()
    assert s.manifest_present is True


def test_status_leaf_present_after_install(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    s = manager.status()
    assert s.leaf_cert_present is True
    assert s.leaf_key_present is True


def test_status_ca_trusted_after_install(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    s = manager.status()
    assert s.ca_trusted is True


def test_status_leaf_valid_until_is_string_after_install(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    s = manager.status()
    assert isinstance(s.leaf_valid_until, str)
    assert len(s.leaf_valid_until) > 0


def test_status_after_uninstall_all_false(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    manager.uninstall()
    # Re-create with is_trusted=False (cert removed from keychain)
    trust_store2 = FakeTrustStore(is_trusted_result=False)
    manager2 = CertManager(trust_store=trust_store2, store_dir=tmp_path)
    s = manager2.status()
    assert s.manifest_present is False
    assert s.ca_trusted is False
    assert s.leaf_cert_present is False


# ---------------------------------------------------------------------------
# leaf_paths()
# ---------------------------------------------------------------------------


def test_leaf_paths_raises_leaf_absent_error_when_no_install(tmp_path):
    manager, _ = _make_manager(is_trusted=False, tmp_path=tmp_path)
    with pytest.raises(LeafAbsentError):
        manager.leaf_paths()


def test_leaf_absent_error_is_exception_subclass():
    assert issubclass(LeafAbsentError, Exception)


def test_leaf_absent_error_message_mentions_cert_install():
    err = LeafAbsentError("leaf not found")
    msg = str(err).lower()
    assert "cert" in msg or "install" in msg, (
        "LeafAbsentError message should hint at running 'cert install'"
    )


def test_leaf_paths_returns_leaf_paths_after_install(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    lp = manager.leaf_paths()
    assert isinstance(lp, LeafPaths)
    assert lp.cert.exists()
    assert lp.key.exists()


def test_leaf_paths_raises_after_uninstall(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    manager.uninstall()
    with pytest.raises(LeafAbsentError):
        manager.leaf_paths()


# ---------------------------------------------------------------------------
# ensure_leaf()
# ---------------------------------------------------------------------------


def test_ensure_leaf_returns_none_when_no_ca_key(tmp_path):
    """When CA key is absent (transient-key default), ensure_leaf returns None."""
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install(persist_ca_key=False)
    result = manager.ensure_leaf()
    assert result is None


def test_ensure_leaf_returns_leaf_paths_when_ca_key_present(tmp_path):
    """When CA key exists and leaf is valid, returns existing LeafPaths."""
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install(persist_ca_key=True)
    result = manager.ensure_leaf()
    assert isinstance(result, LeafPaths)


def test_ensure_leaf_regenerates_when_leaf_missing(tmp_path):
    """ensure_leaf creates a new leaf when the leaf cert is absent but CA key is present."""
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install(persist_ca_key=True)

    # Delete the leaf cert to simulate missing leaf
    result_paths = manager.leaf_paths()
    result_paths.cert.unlink()
    result_paths.key.unlink()

    # ensure_leaf should regenerate
    new_leaf = manager.ensure_leaf()
    assert new_leaf is not None
    assert new_leaf.cert.exists()
    assert new_leaf.key.exists()


# ---------------------------------------------------------------------------
# File permission checks after install
# ---------------------------------------------------------------------------


def test_install_leaf_cert_mode_is_0600(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    result = manager.install()
    assert stat.S_IMODE(result.cert.stat().st_mode) == 0o600


def test_install_leaf_key_mode_is_0600(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    result = manager.install()
    assert stat.S_IMODE(result.key.stat().st_mode) == 0o600


def test_install_ca_cert_mode_is_0600(tmp_path):
    manager, _ = _make_manager(is_trusted=True, tmp_path=tmp_path)
    manager.install()
    ca_cert = tmp_path / "ca.pem"
    assert stat.S_IMODE(ca_cert.stat().st_mode) == 0o600
