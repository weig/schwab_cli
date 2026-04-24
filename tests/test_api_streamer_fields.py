"""Tests for the streamer field-ID decoder."""

from __future__ import annotations

from schwab_cli.api.streamer_fields import (
    decode,
    default_fields,
)


def test_decode_levelone_equities_maps_known_ids():
    content = {
        "key": "NVDA",
        "1": 250.10,
        "2": 250.15,
        "3": 250.12,
        "8": 1_234_567,
    }
    out = decode("LEVELONE_EQUITIES", content)
    assert out["symbol"] == "NVDA"
    assert out["bid"] == 250.10
    assert out["ask"] == 250.15
    assert out["last"] == 250.12
    assert out["volume"] == 1_234_567


def test_decode_passes_through_unknown_ids():
    content = {"key": "NVDA", "999": "unmapped"}
    out = decode("LEVELONE_EQUITIES", content)
    assert out["symbol"] == "NVDA"
    assert out["999"] == "unmapped"


def test_decode_unknown_service_preserves_key_and_raw_fields():
    content = {"key": "XYZ", "1": 123}
    out = decode("MADE_UP_SERVICE", content)
    assert out["symbol"] == "XYZ"
    assert out["1"] == 123


def test_decode_levelone_options_maps_greeks():
    content = {"key": "NVDA260501C250", "27": 0.50, "28": 0.02, "29": -0.05}
    out = decode("LEVELONE_OPTIONS", content)
    assert out["symbol"] == "NVDA260501C250"
    assert out["delta"] == 0.50
    assert out["gamma"] == 0.02
    assert out["theta"] == -0.05


def test_default_fields_includes_symbol():
    assert default_fields("LEVELONE_EQUITIES").startswith("0,")
    assert default_fields("LEVELONE_OPTIONS").startswith("0,")


def test_default_fields_unknown_service_falls_back_to_symbol_only():
    assert default_fields("UNKNOWN") == "0"
