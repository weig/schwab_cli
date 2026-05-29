"""macOS trust-store abstraction over the ``security`` binary.

Defines a structural :class:`TrustStore` protocol and a concrete
:class:`MacTrustStore` that shells out to ``security`` via an injectable
runner (defaults to :func:`subprocess.run`). The runner is injected so tests
never touch the real keychain.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"


class Runner(Protocol):
    """Call signature compatible with :func:`subprocess.run`.

    Injected into :class:`MacTrustStore` so tests never touch the real
    keychain. Must accept an argv list and the ``capture_output``/``text``
    keywords and return a :class:`subprocess.CompletedProcess`-like object
    exposing ``returncode``, ``stdout`` and ``stderr``.
    """

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool = ...,
        text: bool = ...,
    ) -> subprocess.CompletedProcess: ...


class KeychainError(Exception):
    """Raised when a ``security`` invocation fails.

    Carries the captured ``stderr`` of the failed command.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


@runtime_checkable
class TrustStore(Protocol):
    """Structural protocol for OS trust-store operations."""

    def add_trusted_root(self, pem_path: Path | str) -> None: ...

    def is_trusted(self, cn: str) -> bool: ...

    def remove(self, sha256: str) -> None: ...

    def remove_by_label(self, cn: str) -> None: ...


class MacTrustStore:
    """Trust store backed by the macOS ``security`` command."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        try:
            return self._runner(argv, capture_output=True, text=True)
        except Exception as exc:  # noqa: BLE001 - re-wrapped as KeychainError
            raise KeychainError(
                f"Failed to execute: {' '.join(argv)}: {exc}", stderr=str(exc)
            ) from exc

    def add_trusted_root(self, pem_path: Path | str) -> None:
        """Add ``pem_path`` as a trusted root in the System keychain."""
        argv = [
            "sudo",
            "security",
            "add-trusted-cert",
            "-d",
            "-r",
            "trustRoot",
            "-k",
            SYSTEM_KEYCHAIN,
            str(pem_path),
        ]
        result = self._run(argv)
        if result.returncode != 0:
            raise KeychainError(
                f"Failed to add trusted root certificate: {pem_path}",
                stderr=getattr(result, "stderr", "") or "",
            )

    def remove(self, sha256: str) -> None:
        """Delete the certificate matching ``sha256`` from the System keychain.

        ``sha256`` is a plain-hex (no colons) SHA-256 fingerprint used purely
        as a non-cryptographic identifier for the ``security`` CLI. ``-t``
        also drops any associated user trust settings.
        """
        argv = [
            "sudo",
            "security",
            "delete-certificate",
            "-Z",
            sha256,
            "-t",
            SYSTEM_KEYCHAIN,
        ]
        result = self._run(argv)
        if result.returncode != 0:
            raise KeychainError(
                f"Failed to remove certificate with SHA-256 {sha256}",
                stderr=getattr(result, "stderr", "") or "",
            )

    def remove_by_label(self, cn: str) -> None:
        """Delete the certificate matching common name ``cn``."""
        argv = [
            "sudo",
            "security",
            "delete-certificate",
            "-c",
            cn,
            "-t",
            SYSTEM_KEYCHAIN,
        ]
        result = self._run(argv)
        if result.returncode != 0:
            raise KeychainError(
                f"Failed to remove certificate with label {cn!r}",
                stderr=getattr(result, "stderr", "") or "",
            )

    def is_trusted(self, cn: str) -> bool:
        """Return True when a root with common name ``cn`` is admin-trusted.

        Uses ``dump-trust-settings -d`` (the admin domain that
        ``add-trusted-cert -d -r trustRoot`` writes). When no settings exist
        the command prints "No Trust Settings were found." to stderr with
        returncode 0, so trust is determined by ``cn`` appearing in *stdout*,
        not by the returncode.
        """
        argv = ["security", "dump-trust-settings", "-d"]
        result = self._run(argv)
        stdout = getattr(result, "stdout", "") or ""
        return cn in stdout
