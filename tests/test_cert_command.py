"""Tests for ``schwab cert`` command group.

CONTRACT ASSUMED BY THESE TESTS
================================

Module:  ``schwab_cli.commands.cert``
Registered in cli.py as:
    app.add_typer(cert_cmd.app, name="cert")

Subcommands:
    cert status           — print human-readable CertStatus; exit 0.
    cert install          — prompt for confirmation (typer.confirm / stdin).
                            Flags: --yes (skip prompt), --persist-ca-key (passed
                            through to manager.install). Exit 0 on success.
    cert uninstall        — same confirmation pattern.
                            Flags: --yes, --by-label. Exit 0 on success.

Monkeypatchable seam (REQUIRED by implementer):
    schwab_cli.commands.cert._build_manager() -> CertManager

    The cert command module MUST expose a module-level factory function with
    this exact name. The command implementations call ``_build_manager()``
    (not ``CertManager(...)`` directly) so tests can substitute a fake
    manager without touching the real keychain.

    Example implementation skeleton:
        def _build_manager() -> CertManager:
            from schwab_cli.cert.keychain import MacTrustStore
            return CertManager(trust_store=MacTrustStore())

TTY / confirm contract:
    - ``cert install`` without ``--yes``:
        * If stdin is a real TTY (sys.stdin.isatty() is True), the command
          calls typer.confirm (or equivalent) asking the user to proceed.
          "y" → proceeds; "n" → aborts without calling manager.install().
        * If stdin is NOT a TTY (CliRunner default), the command refuses with
          a helpful message and exits non-zero (exit code 1), because running
          ``sudo`` non-interactively without ``--yes`` is ambiguous.
    - ``cert install --yes`` always proceeds regardless of TTY.
    - Same rules apply to ``cert uninstall``.

Platform guard:
    ``cert install`` and ``cert uninstall`` check ``sys.platform`` at
    invocation time. When it is not "darwin" they print a "macOS only" message
    and exit non-zero (exit code 1) WITHOUT calling the manager.
    ``cert status`` is allowed on any platform (informational only).

Exit codes:
    0   — success
    1   — refused (no TTY + no --yes), KeychainError, or non-darwin platform
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.cert.manager import CertStatus, LeafPaths
from schwab_cli.cert.keychain import KeychainError
from schwab_cli.cert.store import ManifestCorruptError

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared fake objects
# ---------------------------------------------------------------------------

_LEAF_CERT = Path("/fake/certs/leaf.crt")
_LEAF_KEY = Path("/fake/certs/leaf.key")
_FAKE_LEAF_PATHS = LeafPaths(cert=_LEAF_CERT, key=_LEAF_KEY)

_FAKE_STATUS_TRUSTED = CertStatus(
    manifest_present=True,
    ca_trusted=True,
    leaf_cert_present=True,
    leaf_key_present=True,
    leaf_valid_until="2027-01-15T00:00:00+00:00",
)

_FAKE_STATUS_NOT_TRUSTED = CertStatus(
    manifest_present=False,
    ca_trusted=False,
    leaf_cert_present=False,
    leaf_key_present=False,
    leaf_valid_until=None,
)


def _make_fake_manager(
    *,
    install_return=_FAKE_LEAF_PATHS,
    install_side_effect=None,
    uninstall_return="Uninstalled Schwab CLI Local CA and removed certificate files.",
    status_return=_FAKE_STATUS_TRUSTED,
):
    """Return a MagicMock that looks like a CertManager with canned responses."""
    m = MagicMock()
    if install_side_effect is not None:
        m.install.side_effect = install_side_effect
    else:
        m.install.return_value = install_return
    m.uninstall.return_value = uninstall_return
    m.status.return_value = status_return
    return m


# ---------------------------------------------------------------------------
# Helper: invoke via the top-level CLI app, patching the seam
# ---------------------------------------------------------------------------

def _invoke(args: list[str], fake_manager, *, input: str | None = None):
    """Invoke the CLI with the given args, patching _build_manager."""
    with patch("schwab_cli.commands.cert._build_manager", return_value=fake_manager):
        return runner.invoke(app, args, input=input)


# ---------------------------------------------------------------------------
# 1. cert status — prints human-readable CertStatus fields
# ---------------------------------------------------------------------------

class TestCertStatus:
    def test_status_shows_trusted(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        # Must call status() exactly once, install/uninstall never called
        fake.status.assert_called_once()
        fake.install.assert_not_called()
        fake.uninstall.assert_not_called()

    def test_status_output_contains_trusted_indicator(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        output_lower = result.output.lower()
        # Output must mention trust state
        assert "trusted" in output_lower

    def test_status_output_contains_leaf_valid_until(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        # The valid-until date must appear in the output
        assert "2027-01-15" in result.output

    def test_status_output_contains_leaf_paths_or_present_flag(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        output_lower = result.output.lower()
        # Output must mention cert/key presence
        assert "leaf" in output_lower or "cert" in output_lower

    def test_status_not_trusted_shows_not_trusted(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_NOT_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        output_lower = result.output.lower()
        # Must indicate not-trusted / not-installed state
        assert "not" in output_lower or "absent" in output_lower or "false" in output_lower

    def test_status_not_trusted_no_valid_until(self):
        fake = _make_fake_manager(status_return=_FAKE_STATUS_NOT_TRUSTED)
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 0, result.output
        # leaf_valid_until is None — output must not show a date or must say "none/absent"
        assert "2027" not in result.output


# ---------------------------------------------------------------------------
# 2. cert install --yes — happy path
# ---------------------------------------------------------------------------

class TestCertInstallYes:
    def test_install_yes_exits_zero(self):
        fake = _make_fake_manager()
        result = _invoke(["cert", "install", "--yes"], fake)

        assert result.exit_code == 0, result.output

    def test_install_yes_calls_manager_install_once(self):
        fake = _make_fake_manager()
        _invoke(["cert", "install", "--yes"], fake)

        fake.install.assert_called_once()

    def test_install_yes_output_mentions_installed_or_trusted(self):
        fake = _make_fake_manager()
        result = _invoke(["cert", "install", "--yes"], fake)

        output_lower = result.output.lower()
        assert "install" in output_lower or "trusted" in output_lower or "success" in output_lower

    def test_install_yes_output_mentions_leaf_path(self):
        fake = _make_fake_manager()
        result = _invoke(["cert", "install", "--yes"], fake)

        # The command should echo the leaf cert path so the user knows where it is
        assert str(_LEAF_CERT) in result.output or "leaf" in result.output.lower()

    def test_install_yes_does_not_call_uninstall_or_status(self):
        fake = _make_fake_manager()
        _invoke(["cert", "install", "--yes"], fake)

        fake.uninstall.assert_not_called()
        fake.status.assert_not_called()


# ---------------------------------------------------------------------------
# 3. cert install without --yes and without TTY — must refuse
# ---------------------------------------------------------------------------

class TestCertInstallNoTtyNoYes:
    def test_install_no_tty_no_yes_does_not_call_manager(self):
        # CliRunner provides no real TTY; sys.stdin.isatty() is False.
        # The command must detect this and refuse without calling install().
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            # Explicitly patch isatty to False to be unambiguous
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = False
                result = runner.invoke(app, ["cert", "install"])

        fake.install.assert_not_called()

    def test_install_no_tty_no_yes_prints_hint(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = False
                result = runner.invoke(app, ["cert", "install"])

        output_lower = result.output.lower()
        # Must mention --yes or terminal so the user knows how to proceed
        assert "--yes" in result.output or "tty" in output_lower or "terminal" in output_lower

    def test_install_no_tty_no_yes_exits_nonzero(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = False
                result = runner.invoke(app, ["cert", "install"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 4. cert install --yes when manager.install() raises KeychainError
# ---------------------------------------------------------------------------

class TestCertInstallKeychainError:
    def test_keychain_error_exits_nonzero(self):
        err = KeychainError("sudo authentication failed", stderr="Password incorrect")
        fake = _make_fake_manager(install_side_effect=err)
        result = _invoke(["cert", "install", "--yes"], fake)

        assert result.exit_code != 0

    def test_keychain_error_exit_code_is_one(self):
        err = KeychainError("sudo authentication failed", stderr="Password incorrect")
        fake = _make_fake_manager(install_side_effect=err)
        result = _invoke(["cert", "install", "--yes"], fake)

        assert result.exit_code == 1

    def test_keychain_error_prints_actionable_message(self):
        err = KeychainError("sudo authentication failed", stderr="Password incorrect")
        fake = _make_fake_manager(install_side_effect=err)
        result = _invoke(["cert", "install", "--yes"], fake)

        output_lower = result.output.lower()
        # Must mention keychain or sudo so the user can act
        assert (
            "keychain" in output_lower
            or "sudo" in output_lower
            or "failed" in output_lower
            or "error" in output_lower
        )

    def test_keychain_error_prints_stderr_detail(self):
        err = KeychainError("sudo authentication failed", stderr="Password incorrect")
        fake = _make_fake_manager(install_side_effect=err)
        result = _invoke(["cert", "install", "--yes"], fake)

        # The stderr detail from KeychainError should surface to the user
        assert "Password incorrect" in result.output or "sudo" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. cert uninstall --yes
# ---------------------------------------------------------------------------

class TestCertUninstallYes:
    def test_uninstall_yes_exits_zero(self):
        fake = _make_fake_manager()
        result = _invoke(["cert", "uninstall", "--yes"], fake)

        assert result.exit_code == 0, result.output

    def test_uninstall_yes_calls_uninstall_without_by_label(self):
        fake = _make_fake_manager()
        _invoke(["cert", "uninstall", "--yes"], fake)

        fake.uninstall.assert_called_once_with(by_label=False)

    def test_uninstall_yes_prints_returned_message(self):
        expected_msg = "Uninstalled Schwab CLI Local CA and removed certificate files."
        fake = _make_fake_manager(uninstall_return=expected_msg)
        result = _invoke(["cert", "uninstall", "--yes"], fake)

        assert result.exit_code == 0, result.output
        assert expected_msg in result.output

    def test_uninstall_yes_does_not_call_install_or_status(self):
        fake = _make_fake_manager()
        _invoke(["cert", "uninstall", "--yes"], fake)

        fake.install.assert_not_called()
        fake.status.assert_not_called()


# ---------------------------------------------------------------------------
# 6. cert uninstall --by-label --yes
# ---------------------------------------------------------------------------

class TestCertUninstallByLabel:
    def test_uninstall_by_label_yes_exits_zero(self):
        fake = _make_fake_manager()
        result = _invoke(["cert", "uninstall", "--by-label", "--yes"], fake)

        assert result.exit_code == 0, result.output

    def test_uninstall_by_label_calls_uninstall_with_by_label_true(self):
        fake = _make_fake_manager()
        _invoke(["cert", "uninstall", "--by-label", "--yes"], fake)

        fake.uninstall.assert_called_once_with(by_label=True)

    def test_uninstall_by_label_prints_returned_message(self):
        by_label_msg = (
            "No manifest found; attempted removal of "
            "'Schwab CLI Local CA' by label and cleaned up any stray files."
        )
        fake = _make_fake_manager(uninstall_return=by_label_msg)
        result = _invoke(["cert", "uninstall", "--by-label", "--yes"], fake)

        assert result.exit_code == 0, result.output
        # Some fragment of the returned message must appear
        assert "label" in result.output or "manifest" in result.output.lower()


# ---------------------------------------------------------------------------
# 7. Platform guard — non-darwin refuses cert install / uninstall
# ---------------------------------------------------------------------------

class TestPlatformGuard:
    def test_install_refuses_on_non_darwin(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "linux"):
                result = runner.invoke(app, ["cert", "install", "--yes"])

        assert result.exit_code != 0
        fake.install.assert_not_called()

    def test_install_non_darwin_prints_macos_only_message(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "linux"):
                result = runner.invoke(app, ["cert", "install", "--yes"])

        output_lower = result.output.lower()
        assert "macos" in output_lower or "darwin" in output_lower or "mac" in output_lower

    def test_install_non_darwin_exit_code_one(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "win32"):
                result = runner.invoke(app, ["cert", "install", "--yes"])

        assert result.exit_code == 1

    def test_uninstall_refuses_on_non_darwin(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "linux"):
                result = runner.invoke(app, ["cert", "uninstall", "--yes"])

        assert result.exit_code != 0
        fake.uninstall.assert_not_called()

    def test_uninstall_non_darwin_prints_macos_only_message(self):
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "linux"):
                result = runner.invoke(app, ["cert", "uninstall", "--yes"])

        output_lower = result.output.lower()
        assert "macos" in output_lower or "darwin" in output_lower or "mac" in output_lower

    def test_status_is_allowed_on_non_darwin(self):
        # cert status is informational — no keychain calls — so it should
        # be permitted on any platform.
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.platform", "linux"):
                result = runner.invoke(app, ["cert", "status"])

        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 8. Confirm behaviour with simulated TTY
# ---------------------------------------------------------------------------

class TestCertInstallConfirmPrompt:
    def test_install_tty_confirm_yes_proceeds(self):
        """Simulated TTY + 'y' input → manager.install() is called."""
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                # Feed 'y\n' as the confirmation answer
                result = runner.invoke(app, ["cert", "install"], input="y\n")

        assert result.exit_code == 0, result.output
        fake.install.assert_called_once()

    def test_install_tty_confirm_no_aborts(self):
        """Simulated TTY + 'n' input → manager.install() is NOT called."""
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                result = runner.invoke(app, ["cert", "install"], input="n\n")

        fake.install.assert_not_called()

    def test_install_tty_confirm_no_does_not_raise(self):
        """Abort should be a clean exit, not an unhandled exception."""
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                result = runner.invoke(app, ["cert", "install"], input="n\n")

        # Either exit 0 (abort is not an error) or 1 (abort is treated as error).
        # Either is acceptable; what matters is no crash.
        assert result.exit_code in (0, 1)
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_uninstall_tty_confirm_yes_proceeds(self):
        """Simulated TTY + 'y' input → manager.uninstall() is called."""
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                result = runner.invoke(app, ["cert", "uninstall"], input="y\n")

        assert result.exit_code == 0, result.output
        fake.uninstall.assert_called_once()

    def test_uninstall_tty_confirm_no_aborts(self):
        """Simulated TTY + 'n' input → manager.uninstall() is NOT called."""
        fake = _make_fake_manager()
        with patch("schwab_cli.commands.cert._build_manager", return_value=fake):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                result = runner.invoke(app, ["cert", "uninstall"], input="n\n")

        fake.uninstall.assert_not_called()


# ---------------------------------------------------------------------------
# 9. ManifestCorruptError — must surface a clean error, never a raw traceback
# ---------------------------------------------------------------------------

_CORRUPT = ManifestCorruptError(
    "Failed to read manifest.json: bad JSON. "
    "Run `schwab cert uninstall` then `schwab cert install` to regenerate it."
)


class TestManifestCorrupt:
    def test_status_corrupt_manifest_exits_1_clean(self):
        fake = MagicMock()
        fake.status.side_effect = _CORRUPT
        result = _invoke(["cert", "status"], fake)

        assert result.exit_code == 1, result.output
        # Actionable message present; no raw traceback leaked.
        assert "manifest" in result.output.lower()
        assert "Traceback" not in result.output

    def test_install_corrupt_manifest_exits_1_clean(self):
        fake = _make_fake_manager(install_side_effect=_CORRUPT)
        result = _invoke(["cert", "install", "--yes"], fake)

        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output

    def test_uninstall_corrupt_manifest_exits_1_clean(self):
        fake = MagicMock()
        fake.uninstall.side_effect = _CORRUPT
        result = _invoke(["cert", "uninstall", "--yes"], fake)

        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
