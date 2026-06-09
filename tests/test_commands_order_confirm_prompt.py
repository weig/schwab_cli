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


# ---- ConfirmRule: live-ticker data source (daemon stream vs REST) ----------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from schwab_cli.order_pipeline.context import OrderContext  # noqa: E402
from schwab_cli.order_pipeline.rules import ConfirmRule  # noqa: E402


def _confirm_ctx(**over):
    base = dict(
        spec=None, body={}, account=SimpleNamespace(account_number="123456789"),
        client=object(), sub="place", dry_run=False, yes=False, overriding=False,
        profile_name="p", override_reason=None, as_json=False, limits=None,
        underlying_quote={"symbol": "SPY", "last": 1.0},
    )
    base.update(over)
    return OrderContext(**base)


def test_confirm_rule_streams_via_daemon_when_reachable():
    with patch("schwab_cli.commands.order._confirm_or_abort"), \
         patch("schwab_cli.commands.order._fetch_underlying_quote_safe",
               return_value={"symbol": "SPY", "last": 2.0}), \
         patch("schwab_cli.commands._stream_mcp.probe_daemon",
               return_value=True), \
         patch("schwab_cli.order_pipeline.live_ticker.StreamQuoteSource") as SQS, \
         patch("schwab_cli.order_pipeline.live_ticker.LiveTicker") as LT:
        ConfirmRule().execute(_confirm_ctx())
    # Stream source started and torn down; ticker driven by it.
    SQS.assert_called_once()
    SQS.return_value.start.assert_called_once()
    SQS.return_value.stop.assert_called_once()
    LT.return_value.start.assert_called_once()
    LT.return_value.stop.assert_called_once()


def test_confirm_rule_rest_only_when_no_daemon():
    with patch("schwab_cli.commands.order._confirm_or_abort"), \
         patch("schwab_cli.commands.order._fetch_underlying_quote_safe",
               return_value={"symbol": "SPY"}), \
         patch("schwab_cli.commands._stream_mcp.probe_daemon",
               return_value=False), \
         patch("schwab_cli.order_pipeline.live_ticker.StreamQuoteSource") as SQS, \
         patch("schwab_cli.order_pipeline.live_ticker.LiveTicker") as LT:
        ConfirmRule().execute(_confirm_ctx())
    SQS.assert_not_called()   # never streams
    LT.return_value.start.assert_called_once()
    LT.return_value.stop.assert_called_once()


def test_confirm_rule_skips_ticker_when_yes_flag():
    # --yes (non-override) skips the prompt → no ticker, no probe.
    with patch("schwab_cli.commands.order._confirm_or_abort"), \
         patch("schwab_cli.commands._stream_mcp.probe_daemon") as probe, \
         patch("schwab_cli.order_pipeline.live_ticker.LiveTicker") as LT:
        ConfirmRule().execute(_confirm_ctx(yes=True))
    probe.assert_not_called()
    LT.assert_not_called()
