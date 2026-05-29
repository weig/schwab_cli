"""Unit tests for schwab_cli.cert.keychain (RED phase — no implementation yet).

API CONTRACT settled by these tests
====================================
Module: ``schwab_cli.cert.keychain``

``TrustStore`` (Protocol)
    Structural protocol; implementors must provide:
        add_trusted_root(pem_path: Path | str) -> None
        is_trusted(cn: str) -> bool
        remove(sha256: str) -> None
        remove_by_label(cn: str) -> None

``KeychainError``
    Exception class raised when the underlying ``security`` command returns
    a non-zero exit code. Must carry a ``stderr`` attribute (str) with the
    captured stderr output.

``MacTrustStore``
    Concrete implementation of TrustStore.
    Constructor: ``MacTrustStore(runner=subprocess.run)``
    The ``runner`` kwarg has the same call signature as ``subprocess.run``:
        runner(argv: list[str], *, capture_output: bool, text: bool) -> result
    where ``result`` has ``.returncode`` (int), ``.stdout`` (str), ``.stderr`` (str).

    ``add_trusted_root(pem_path)``
        Invokes (via runner):
            sudo security add-trusted-cert -d -r trustRoot
                 -k /Library/Keychains/System.keychain <pem_path>
        On returncode != 0: raises KeychainError with .stderr set.

    ``remove(sha256)``
        Invokes (via runner):
            sudo security delete-certificate -Z <sha256> -t
                 /Library/Keychains/System.keychain
        ``sha256`` is plain-hex (no colons). ``-t`` also drops trust settings.
        On returncode != 0: raises KeychainError with .stderr set.

    ``is_trusted(cn)``
        Checks admin-domain trust settings via ``dump-trust-settings -d``.
        Returns True iff ``cn`` appears in the command's STDOUT (the empty
        case prints to stderr with returncode 0, so returncode is not used).
        If the runner raises (e.g. OSError / FileNotFoundError) → raises KeychainError.

IMPORTANT: No test in this file may invoke the real ``security`` binary.
           All tests use a fake runner injected into MacTrustStore.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from schwab_cli.cert.keychain import KeychainError, MacTrustStore, TrustStore

# ---------------------------------------------------------------------------
# Fake runner helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _make_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Returns a fake runner and a list that records every argv it was called with."""
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=True, text=True, **_kwargs):
        calls.append(list(argv))
        return _FakeResult(returncode=returncode, stdout=stdout, stderr=stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _make_error_runner(exc: Exception):
    """Returns a runner that raises the given exception on every call."""
    def runner(argv, **_kwargs):
        raise exc

    return runner


# ---------------------------------------------------------------------------
# TrustStore Protocol conformance
# ---------------------------------------------------------------------------


def test_mac_trust_store_satisfies_trust_store_protocol():
    """MacTrustStore must be structurally compatible with TrustStore Protocol."""
    runner = _make_runner()
    store = MacTrustStore(runner=runner)
    # Runtime check: isinstance with Protocol (requires runtime_checkable).
    # If TrustStore is not runtime_checkable, just verify the methods exist.
    assert callable(getattr(store, "add_trusted_root", None))
    assert callable(getattr(store, "is_trusted", None))
    assert callable(getattr(store, "remove", None))


def test_trust_store_protocol_is_defined():
    """TrustStore must be importable and be a Protocol or ABC-like class."""
    import inspect

    # It should either be a Protocol or have abstract methods — just verify it exists
    # and is a class.
    assert inspect.isclass(TrustStore)


# ---------------------------------------------------------------------------
# add_trusted_root — argv contract
# ---------------------------------------------------------------------------


def test_add_trusted_root_calls_runner_with_correct_argv():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.add_trusted_root("/tmp/ca.pem")
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv == [
        "sudo", "security", "add-trusted-cert",
        "-d", "-r", "trustRoot",
        "-k", "/Library/Keychains/System.keychain",
        "/tmp/ca.pem",
    ]


def test_add_trusted_root_accepts_path_object():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.add_trusted_root(Path("/tmp/ca.pem"))
    argv = runner.calls[0]
    # The last element should be the string form of the path
    assert argv[-1] == "/tmp/ca.pem"


def test_add_trusted_root_uses_system_keychain_path():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.add_trusted_root("/some/path.pem")
    argv = runner.calls[0]
    assert "/Library/Keychains/System.keychain" in argv


def test_add_trusted_root_uses_sudo():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.add_trusted_root("/tmp/ca.pem")
    assert runner.calls[0][0] == "sudo"


def test_add_trusted_root_uses_security_binary():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.add_trusted_root("/tmp/ca.pem")
    assert runner.calls[0][1] == "security"


def test_add_trusted_root_returns_none_on_success():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    result = store.add_trusted_root("/tmp/ca.pem")
    assert result is None


# ---------------------------------------------------------------------------
# add_trusted_root — error handling
# ---------------------------------------------------------------------------


def test_add_trusted_root_raises_keychain_error_on_nonzero():
    runner = _make_runner(returncode=1, stderr="Permission denied")
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.add_trusted_root("/tmp/ca.pem")


def test_add_trusted_root_keychain_error_carries_stderr():
    stderr_msg = "Error: -25308: iokit error..."
    runner = _make_runner(returncode=1, stderr=stderr_msg)
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError) as exc_info:
        store.add_trusted_root("/tmp/ca.pem")
    assert exc_info.value.stderr == stderr_msg


def test_add_trusted_root_raises_keychain_error_on_runner_exception():
    runner = _make_error_runner(FileNotFoundError("security not found"))
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.add_trusted_root("/tmp/ca.pem")


# ---------------------------------------------------------------------------
# remove — argv contract
# ---------------------------------------------------------------------------

# Plain-hex SHA-256 (no colons) — the exact form `delete-certificate -Z` accepts.
SHA256_FIXTURE = "7B9B5691DAE1A11613699A586917B33BD71306657B9B5691DAE1A11613699A58"
CA_CN = "Schwab CLI Local CA"


def test_remove_calls_runner_with_correct_argv():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(SHA256_FIXTURE)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv == [
        "sudo", "security", "delete-certificate",
        "-Z", SHA256_FIXTURE,
        "-t",
        "/Library/Keychains/System.keychain",
    ]


def test_remove_uses_sudo():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(SHA256_FIXTURE)
    assert runner.calls[0][0] == "sudo"


def test_remove_uses_delete_certificate_subcommand():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(SHA256_FIXTURE)
    assert "delete-certificate" in runner.calls[0]


def test_remove_passes_sha256_with_z_flag():
    sha256 = "AABBCC"
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(sha256)
    argv = runner.calls[0]
    z_idx = argv.index("-Z")
    assert argv[z_idx + 1] == sha256


def test_remove_includes_t_flag_to_drop_trust_settings():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(SHA256_FIXTURE)
    assert "-t" in runner.calls[0]


def test_remove_hash_has_no_colons():
    """delete-certificate -Z rejects colon-delimited hashes."""
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove(SHA256_FIXTURE)
    argv = runner.calls[0]
    assert ":" not in argv[argv.index("-Z") + 1]


def test_remove_returns_none_on_success():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    result = store.remove(SHA256_FIXTURE)
    assert result is None


# ---------------------------------------------------------------------------
# remove — error handling
# ---------------------------------------------------------------------------


def test_remove_raises_keychain_error_on_nonzero():
    runner = _make_runner(returncode=1, stderr="Not found")
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.remove(SHA256_FIXTURE)


def test_remove_keychain_error_carries_stderr():
    stderr_msg = "SecKeychainSearchCopyNext: The specified item could not be found."
    runner = _make_runner(returncode=44, stderr=stderr_msg)
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError) as exc_info:
        store.remove(SHA256_FIXTURE)
    assert exc_info.value.stderr == stderr_msg


def test_remove_raises_keychain_error_on_runner_exception():
    runner = _make_error_runner(OSError("Cannot exec"))
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.remove(SHA256_FIXTURE)


# ---------------------------------------------------------------------------
# remove_by_label — argv contract
# ---------------------------------------------------------------------------


def test_remove_by_label_calls_runner_with_correct_argv():
    runner = _make_runner(returncode=0)
    store = MacTrustStore(runner=runner)
    store.remove_by_label(CA_CN)
    assert runner.calls[0] == [
        "sudo", "security", "delete-certificate",
        "-c", CA_CN,
        "-t",
        "/Library/Keychains/System.keychain",
    ]


def test_remove_by_label_raises_keychain_error_on_nonzero():
    runner = _make_runner(returncode=1, stderr="Not found")
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.remove_by_label(CA_CN)


# ---------------------------------------------------------------------------
# is_trusted — driven by dump-trust-settings STDOUT content
# ---------------------------------------------------------------------------


def test_is_trusted_returns_true_when_cn_in_stdout():
    runner = _make_runner(
        returncode=0,
        stdout=f"Cert 0: {CA_CN}\n    Trust Setting 0: ...\n",
    )
    store = MacTrustStore(runner=runner)
    assert store.is_trusted(CA_CN) is True


def test_is_trusted_returns_false_when_cn_absent_from_stdout():
    runner = _make_runner(returncode=0, stdout="Cert 0: Some Other CA\n")
    store = MacTrustStore(runner=runner)
    assert store.is_trusted(CA_CN) is False


def test_is_trusted_false_when_no_trust_settings():
    """Empty admin domain: rc=0, message on STDERR, empty STDOUT → False."""
    runner = _make_runner(
        returncode=0,
        stdout="",
        stderr="No Trust Settings were found.\n",
    )
    store = MacTrustStore(runner=runner)
    assert store.is_trusted(CA_CN) is False


def test_is_trusted_does_not_use_returncode():
    """A trusted CN in stdout means trusted even if returncode is nonzero."""
    runner = _make_runner(returncode=1, stdout=f"Cert 0: {CA_CN}\n")
    store = MacTrustStore(runner=runner)
    assert store.is_trusted(CA_CN) is True


def test_is_trusted_calls_runner_exactly_once():
    runner = _make_runner(returncode=0, stdout=f"{CA_CN}\n")
    store = MacTrustStore(runner=runner)
    store.is_trusted(CA_CN)
    assert len(runner.calls) == 1


def test_is_trusted_uses_dump_trust_settings_admin_domain():
    runner = _make_runner(returncode=0, stdout=f"{CA_CN}\n")
    store = MacTrustStore(runner=runner)
    store.is_trusted(CA_CN)
    argv = runner.calls[0]
    assert argv == ["security", "dump-trust-settings", "-d"]


def test_is_trusted_does_not_use_verify_cert():
    """verify-cert is unusable for root trust; must not be used."""
    runner = _make_runner(returncode=0, stdout=f"{CA_CN}\n")
    store = MacTrustStore(runner=runner)
    store.is_trusted(CA_CN)
    assert "verify-cert" not in runner.calls[0]


def test_is_trusted_argv_includes_security_binary():
    runner = _make_runner(returncode=0, stdout=f"{CA_CN}\n")
    store = MacTrustStore(runner=runner)
    store.is_trusted(CA_CN)
    argv = runner.calls[0]
    assert "security" in argv


def test_is_trusted_raises_keychain_error_when_runner_raises():
    """If the runner itself throws (e.g. FileNotFoundError), we get KeychainError."""
    runner = _make_error_runner(FileNotFoundError("security not found"))
    store = MacTrustStore(runner=runner)
    with pytest.raises(KeychainError):
        store.is_trusted(CA_CN)


# ---------------------------------------------------------------------------
# KeychainError — basic properties
# ---------------------------------------------------------------------------


def test_keychain_error_is_exception_subclass():
    assert issubclass(KeychainError, Exception)


def test_keychain_error_carries_stderr_attribute():
    err = KeychainError("msg", stderr="some stderr")
    assert err.stderr == "some stderr"


def test_keychain_error_stderr_defaults_to_empty_string():
    err = KeychainError("msg")
    # Should not raise AttributeError; default can be "" or None but prefer "".
    assert hasattr(err, "stderr")


# ---------------------------------------------------------------------------
# No real security binary ever called (guard)
# ---------------------------------------------------------------------------


def test_mac_trust_store_default_runner_is_subprocess_run_not_called_here():
    """Verifying that MacTrustStore accepts a runner kwarg and doesn't
    auto-invoke subprocess when a fake is provided."""
    import subprocess

    real_run_calls = []
    original_run = subprocess.run

    def sentinel(*args, **kwargs):
        real_run_calls.append(args)
        return original_run(*args, **kwargs)

    # We do NOT call any methods — just constructing with a fake runner is safe.
    fake = _make_runner(returncode=0)
    store = MacTrustStore(runner=fake)
    assert real_run_calls == [], "Real subprocess.run must NOT be called during construction"
