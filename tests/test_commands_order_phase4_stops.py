"""Phase 4 — STOP / STOP_LIMIT / TRAILING_STOP* / MOC / LOC / EXERCISE.

Each variant either adds new body fields (stopPrice, stopPriceOffset,
…) or just relies on orderType passing through. Tests assert on the
JSON body emitted by ``order place --dry-run --json`` so we exercise
the whole spec → body pipeline without hitting Schwab.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from schwab_cli.cli import app
# Reuse the heavy fixture machinery (config, session, account
# resolution, API patches) from the existing big order test file.
from tests.test_commands_order import (
    _prep, _patches, _enter_all, _exit_all,
)


runner = CliRunner()


# ---- STOP --------------------------------------------------------------


def test_stop_order_writes_stop_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "STOP", "--side", "SELL",
            "--stop-price", "145.50",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "STOP"
    assert body["stopPrice"] == "145.50"
    # Plain STOP doesn't carry a limit price.
    assert "price" not in body


def test_stop_order_requires_stop_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "STOP", "--side", "SELL", "--dry-run",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code != 0
    assert "requires --stop-price" in (result.output + result.stderr)


# ---- STOP_LIMIT --------------------------------------------------------


def test_stop_limit_writes_both_prices(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "STOP_LIMIT", "--side", "SELL",
            "--stop-price", "145.50",        # trigger
            "--price", "144.00",             # limit once triggered
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "STOP_LIMIT"
    assert body["stopPrice"] == "145.50"
    assert body["price"] == "144.00"


def test_stop_limit_requires_both_prices(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        # Missing --price.
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "STOP_LIMIT", "--side", "SELL",
            "--stop-price", "145.50", "--dry-run",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code != 0
    assert "STOP_LIMIT requires both" in (result.output + result.stderr)


# ---- TRAILING_STOP -----------------------------------------------------


def test_trailing_stop_writes_offset_and_basis(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "TRAILING_STOP", "--side", "SELL",
            "--trailing-offset", "1.50",
            "--trailing-basis", "LAST",
            "--trailing-type", "VALUE",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "TRAILING_STOP"
    assert body["stopPriceOffset"] == 1.50
    assert body["stopPriceLinkBasis"] == "LAST"
    assert body["stopPriceLinkType"] == "VALUE"
    # Plain TRAILING_STOP doesn't carry a fixed limit price.
    assert "price" not in body


def test_trailing_stop_requires_offset_basis_type(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "TRAILING_STOP", "--side", "SELL",
            "--trailing-offset", "1.50", "--dry-run",
            # Missing --trailing-basis and --trailing-type.
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code != 0
    msg = result.output + result.stderr
    assert "--trailing-basis" in msg
    assert "--trailing-type" in msg


def test_trailing_stop_limit_writes_full_body(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "TRAILING_STOP_LIMIT", "--side", "SELL",
            "--trailing-offset", "5",
            "--trailing-basis", "MARK",
            "--trailing-type", "PERCENT",
            "--price", "143.00",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "TRAILING_STOP_LIMIT"
    assert body["stopPriceOffset"] == 5
    assert body["stopPriceLinkBasis"] == "MARK"
    assert body["stopPriceLinkType"] == "PERCENT"
    assert body["price"] == "143.00"


# ---- MARKET_ON_CLOSE / LIMIT_ON_CLOSE ---------------------------------


def test_market_on_close_passes_through(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "MARKET_ON_CLOSE", "--side", "BUY",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "MARKET_ON_CLOSE"
    # MOC carries no price, no stop fields.
    assert "price" not in body
    assert "stopPrice" not in body


def test_limit_on_close_requires_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT_ON_CLOSE", "--side", "BUY",
            "--dry-run",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code != 0
    assert "LIMIT_ON_CLOSE requires --price" in (result.output + result.stderr)


def test_limit_on_close_with_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT_ON_CLOSE", "--side", "BUY",
            "--price", "150.00",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "LIMIT_ON_CLOSE"
    assert body["price"] == "150.00"


# ---- EXERCISE ----------------------------------------------------------


def test_exercise_passes_through(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "EXERCISE", "--side", "BUY",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    body = json.loads(result.stdout)["order"]
    assert body["orderType"] == "EXERCISE"
    assert "price" not in body
    assert "stopPrice" not in body
