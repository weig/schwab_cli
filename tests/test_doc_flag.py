"""Tests for the ``--doc`` flag wiring.

Every command + the top-level app should accept ``--doc`` and emit both
Click's auto-generated help and the matching markdown page from
``doc/<name>.md``.
"""

from typer.testing import CliRunner

from schwab_cli.cli import app

runner = CliRunner()


def _has_doc_marker(output: str, marker: str) -> bool:
    """Assert that a distinctive line from the doc page appears in output."""
    return marker in output


def test_doc_flag_on_empty_command_shows_index_page():
    """`schwab_cli --doc` dumps doc/index.md below the app-level help."""
    result = runner.invoke(app, ["--doc"])
    assert result.exit_code == 0, result.output
    # Click help surfaces the subcommand list.
    assert "Usage:" in result.output
    # Index page carries the install section heading.
    assert "## Install" in result.output
    # Separator between help and doc.
    assert "=" * 10 in result.output


def test_doc_flag_on_vol_shows_vol_page():
    result = runner.invoke(app, ["vol", "--doc"])
    assert result.exit_code == 0, result.output
    assert "vol " in result.output or "vol\n" in result.output
    # Distinctive phrasing from doc/vol.md:
    assert "HV percentile" in result.output or "IVP state machine" in result.output


def test_doc_flag_on_history_shows_history_page():
    result = runner.invoke(app, ["history", "--doc"])
    assert result.exit_code == 0, result.output
    assert "## Range examples" in result.output


def test_doc_flag_on_greeks_shows_greeks_page():
    result = runner.invoke(app, ["greeks", "--doc"])
    assert result.exit_code == 0, result.output
    assert "Black-Scholes" in result.output or "break-even" in result.output.lower()


def test_doc_flag_on_skew_shows_skew_page():
    result = runner.invoke(app, ["skew", "--doc"])
    assert result.exit_code == 0, result.output
    # Distinctive phrasing from doc/skew.md:
    assert "Risk Reversal" in result.output or "25Δ" in result.output
    assert "--cross" in result.output


def test_doc_flag_on_auth_shows_auth_page():
    result = runner.invoke(app, ["auth", "--doc"])
    assert result.exit_code == 0, result.output
    # The auth page documents both flows + the env vars.
    assert "code_relay" in result.output
    assert "HEADLESS" in result.output


def test_doc_flag_does_not_run_the_command():
    """--doc should exit before the command body runs — no API calls, no
    session lookup, nothing that would require a configured account."""
    # If --doc leaked into the body, `vol` would error trying to read
    # config/session because we haven't prepped them.
    result = runner.invoke(app, ["vol", "--doc"])
    assert result.exit_code == 0, result.output
    # No "No config found" or similar — we exited early inside the callback.
    assert "No config" not in result.output
    assert "No session" not in result.output


def test_help_still_works_without_doc():
    """--help keeps its normal behaviour — no markdown appended."""
    result = runner.invoke(app, ["vol", "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    # doc/vol.md's distinctive heading should NOT appear on plain --help.
    assert "IVP state machine" not in result.output
    assert "Local storage" not in result.output
