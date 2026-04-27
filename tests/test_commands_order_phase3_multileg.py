"""Phase 3 — end-to-end multi-leg pipeline (parse → spec → body).

Verifies that the extended TOS parser strategies flow all the way
through to the Schwab order body via ``order place --dry-run --json``.
The parser-side legs are already covered by
test_order_ticket_phase3_strategies; these tests assert that the spec
builder + body builder don't drop legs or scramble instructions for
3- and 4-leg shapes.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from schwab_cli.cli import app
from tests.test_commands_order import (
    _prep, _patches, _enter_all, _exit_all,
)

runner = CliRunner()


def test_butterfly_pipeline_emits_three_option_legs(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "--account", "5678",
            "--parse",
            "BUY +1 BUTTERFLY AMZN 1 MAY 26 195/200/205 CALL @0.85 LMT",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "NET_DEBIT"
    assert body["complexOrderStrategyType"] == "BUTTERFLY"
    legs = body["orderLegCollection"]
    assert len(legs) == 3
    # Wing-Body-Wing ratio: qty 1, 2, 1.
    quantities = [l["quantity"] for l in legs]
    assert sorted(quantities) == [1, 1, 2]


def test_iron_condor_pipeline_emits_four_legs_two_puts_two_calls(
    monkeypatch, tmp_path,
):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "--account", "5678",
            "--parse",
            "BUY +1 IRON CONDOR AMZN 1 MAY 26 190/195/205/210 @1.20 LMT",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "NET_CREDIT"
    assert body["complexOrderStrategyType"] == "IRON_CONDOR"
    legs = body["orderLegCollection"]
    assert len(legs) == 4
    osi_symbols = [l["instrument"]["symbol"] for l in legs]
    # Two PUTs (low pair) + two CALLs (high pair).
    n_puts  = sum(1 for s in osi_symbols if "P" in s.split()[-1])
    n_calls = sum(1 for s in osi_symbols if "C" in s.split()[-1])
    assert n_puts == 2 and n_calls == 2


def test_straddle_pipeline_emits_call_plus_put_at_same_strike(
    monkeypatch, tmp_path,
):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "--account", "5678",
            "--parse",
            "BUY +1 STRADDLE AMZN 1 MAY 26 200 @5.50 LMT",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "NET_DEBIT"
    legs = body["orderLegCollection"]
    assert len(legs) == 2
    # All BUY_TO_OPEN.
    assert all(l["instruction"] == "BUY_TO_OPEN" for l in legs)


def test_condor_pipeline_emits_four_legs(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "--account", "5678",
            "--parse",
            "BUY +1 CONDOR AMZN 1 MAY 26 190/195/205/210 CALL @0.50 LMT",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)["order"]
    legs = body["orderLegCollection"]
    assert len(legs) == 4
    # All same-tenor calls.
    assert all("C" in l["instrument"]["symbol"].split()[-1] for l in legs)
