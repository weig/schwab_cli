"""Tests for the order-place confirmation prompt (`_confirm_or_abort`).

The live-ticker thread refreshes a status line above the prompt every
~1.5s while the user decides. Empty input (just pressing Enter without
typing) used to abort — operators reported it as a "crash" because
they expected Enter to be a no-op and let the ticker keep updating.
This test pins the new behavior:

* ``yes`` confirms (returns)
* ``""`` (just newline) re-prompts — does NOT abort
* EOF aborts
* Any non-empty non-yes input aborts
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
import typer

from schwab_cli.commands.order import _confirm_or_abort


def _stdin(text: str) -> io.StringIO:
    """Build a fake stdin whose ``readline`` mirrors real terminal
    behavior: each call consumes one line ending in ``\\n``; final
    call after exhaustion returns ``""`` (EOF)."""
    return io.StringIO(text)


def test_confirm_with_explicit_yes_returns_silently():
    with patch("sys.stdin", _stdin("yes\n")):
        _confirm_or_abort(yes=False)  # must not raise


def test_confirm_case_insensitive_yes():
    with patch("sys.stdin", _stdin("YES\n")):
        _confirm_or_abort(yes=False)


def test_yes_flag_skips_prompt_entirely():
    # Empty stdin — must not block on readline.
    with patch("sys.stdin", _stdin("")):
        _confirm_or_abort(yes=True)


def test_blank_enter_re_prompts_until_yes():
    """Operator presses Enter twice while reading the live ticker,
    then types yes. Must NOT abort on the blank lines."""
    fake = _stdin("\n\n\nyes\n")
    with patch("sys.stdin", fake):
        _confirm_or_abort(yes=False)


def test_eof_after_blanks_aborts_cleanly():
    """If stdin closes (e.g. piped input ends) while the operator is
    holding the prompt with blanks, abort rather than spinning."""
    fake = _stdin("\n\n")  # two blanks then EOF
    with patch("sys.stdin", fake):
        with pytest.raises(typer.Exit) as exc:
            _confirm_or_abort(yes=False)
        assert int(exc.value.exit_code or 0) == 0


def test_explicit_no_aborts():
    with patch("sys.stdin", _stdin("no\n")):
        with pytest.raises(typer.Exit) as exc:
            _confirm_or_abort(yes=False)
        assert int(exc.value.exit_code or 0) == 0


@pytest.mark.parametrize("text", ["y\n", "yep\n", "confirm\n", "yes please\n"])
def test_random_text_aborts(text):
    """``y``, ``yep``, ``confirm`` — any non-blank non-yes still aborts.
    Strict-yes is the safety guarantee for live trading."""
    with patch("sys.stdin", _stdin(text)):
        with pytest.raises(typer.Exit) as exc:
            _confirm_or_abort(yes=False)
        assert int(exc.value.exit_code or 0) == 0
