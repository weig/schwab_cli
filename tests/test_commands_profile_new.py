"""End-to-end tests for `profile new --type=order`.

Phase 2f-4 ships the interactive command. The TTY-driven flow can't
be exercised in CliRunner without a pseudo-terminal, so we cover:

- Non-TTY exits 2 with a helpful pointer.
- --type validation still kicks in (already covered by parent CLI tests
  but we re-assert here to lock the surface).
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app


runner = CliRunner()


def test_profile_new_requires_type():
    result = runner.invoke(app, ["profile", "new"])
    assert result.exit_code == 2
    assert "Missing option '--type'" in result.stderr or \
           "Missing option '--type'" in result.stdout


def test_profile_new_rejects_other_type():
    result = runner.invoke(app, ["profile", "new", "--type", "notification"])
    assert result.exit_code == 2
    assert "must be 'order'" in result.stderr


def test_profile_new_non_tty_exits_2_with_pointer():
    """When stdin isn't a TTY, the interactive command refuses and
    points at hand-authoring."""
    with patch("schwab_cli.order_policy.profile_new.editor.sys.stdin") as fake_stdin:
        fake_stdin.isatty.return_value = False
        result = runner.invoke(app, ["profile", "new", "--type", "order"])
    assert result.exit_code == 2
    out = result.stderr + result.stdout
    assert "interactive TTY" in out
    assert "by hand" in out
