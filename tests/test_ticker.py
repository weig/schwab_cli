"""Tests for the ticker resolver.

The resolver has two jobs:
  - Parse any reasonable user-facing ticker string into a structured `Ticker`.
  - Emit the canonical OSI-padded symbol that Schwab's API expects.

All four listed option-input formats for the same contract must resolve to
equal `Ticker` instances, and all four must round-trip through
`to_schwab_symbol` to the same canonical string.
"""

import pytest

from schwab_cli.ticker import OptionPart, Ticker, TickerError, resolve


# ---- stock --------------------------------------------------------------


def test_resolve_plain_stock():
    t = resolve("NVDA")
    assert t == Ticker(type="stock", underlying="NVDA", option=None)


def test_resolve_stock_lowercase_gets_normalized():
    assert resolve("nvda").underlying == "NVDA"


def test_resolve_stock_class_share_normalizes_to_slash():
    """Schwab's quote / chain / history endpoints want ``BRK/B`` —
    the dot and dash forms silently return empty payloads. The
    resolver canonicalizes to slash so downstream API calls always
    use the form Schwab accepts."""
    expected = Ticker(type="stock", underlying="BRK/B", option=None)
    assert resolve("BRK.B") == expected
    assert resolve("BRK/B") == expected
    assert resolve("BRK-B") == expected
    assert resolve("brk.b") == expected   # case-insensitive


def test_stock_ticker_to_schwab_symbol_is_identity():
    assert resolve("NVDA").to_schwab_symbol() == "NVDA"


@pytest.mark.parametrize("sym", ["$SPX", "$XSP", "$NDX", "$RUT", "$VIX", "$VIX3M"])
def test_resolve_index_preserves_dollar_prefix(sym):
    """Cash-settled index underlyings must pass through with the ``$``
    intact — Schwab's chain/quote/history endpoints require it (``SPX``
    without the ``$`` 400s)."""
    t = resolve(sym)
    assert t == Ticker(type="stock", underlying=sym, option=None)
    assert t.to_schwab_symbol() == sym  # identity, $ preserved


def test_resolve_index_case_insensitive():
    assert resolve("$xsp").underlying == "$XSP"


def test_to_schwab_form_preserves_index_dollar():
    from schwab_cli.ticker import to_schwab_form
    assert to_schwab_form("$SPX") == "$SPX"
    assert to_schwab_form("$xsp") == "$XSP"


def test_to_schwab_form_normalizes_class_shares():
    """Module-level helper used at API boundaries when the caller
    has a raw symbol string and didn't go through the Ticker
    resolver. Idempotent and OSI-safe."""
    from schwab_cli.ticker import to_schwab_form
    assert to_schwab_form("BRK.B") == "BRK/B"
    assert to_schwab_form("BRK-B") == "BRK/B"
    assert to_schwab_form("BRK/B") == "BRK/B"   # idempotent
    assert to_schwab_form("NVDA") == "NVDA"     # plain pass-through
    # OSI option strings have digits; must not get rewritten.
    assert to_schwab_form("NVDA  260501C00240000") == "NVDA  260501C00240000"
    # Lowercase input is uppercased so renderer key lookups against
    # the Schwab response (always uppercase keys) match. Without this,
    # `quote amzn` shows all dashes because payload.get("amzn") misses.
    assert to_schwab_form("amzn") == "AMZN"
    assert to_schwab_form("brk.b") == "BRK/B"


# ---- option: all four input forms equivalent -----------------------------


_EXPECTED = Ticker(
    type="option",
    underlying="NVDA",
    option=OptionPart(date="20260501", type="C", strike=240.0),
)


@pytest.mark.parametrize(
    "raw",
    [
        "NVDA260501C240",           # compact, integer strike
        "NVDA  260501C240",          # Schwab-style padding, integer strike
        "NVDA260501C240.0",          # decimal strike
        "NVDA260501C00240000",       # full OSI 8-digit strike * 1000
        "NVDA 260501C240",           # single space
        "nvda260501c240",            # lowercase
        " NVDA260501C240 ",          # surrounding whitespace
    ],
)
def test_resolve_option_all_input_forms_equivalent(raw):
    assert resolve(raw) == _EXPECTED


def test_resolve_option_put():
    t = resolve("NVDA260501P240")
    assert t.type == "option"
    assert t.option == OptionPart(date="20260501", type="P", strike=240.0)


def test_resolve_option_fractional_strike_decimal_form():
    t = resolve("NVDA260501C202.5")
    assert t.option == OptionPart(date="20260501", type="C", strike=202.5)


def test_resolve_option_fractional_strike_osi_form():
    # 202.500 strike → 202500 → padded to 8 digits = "00202500"
    t = resolve("NVDA260501C00202500")
    assert t.option == OptionPart(date="20260501", type="C", strike=202.5)


def test_resolve_option_low_strike():
    # A $5 strike option — OSI form is 00005000
    compact = resolve("NVDA260501C5")
    osi = resolve("NVDA260501C00005000")
    assert compact == osi
    assert compact.option.strike == 5.0


# ---- canonical Schwab symbol -------------------------------------------


def test_option_canonical_symbol_padded_to_6_chars():
    # "NVDA" is 4 chars — Schwab format pads underlying to 6 with spaces.
    assert resolve("NVDA260501C240").to_schwab_symbol() == "NVDA  260501C00240000"


def test_option_canonical_symbol_5_char_underlying_padded_to_6():
    assert resolve("GOOGL260501C240").to_schwab_symbol() == "GOOGL 260501C00240000"


def test_option_canonical_symbol_already_6_char_underlying_not_padded():
    # Some symbols are already 6 chars (rare; digit-bearing underlying is
    # the interesting case — verifies the lazy regex backtracks to the
    # longest valid underlying that still lets the date anchor win).
    assert resolve("EXACT6260501C240").to_schwab_symbol() == "EXACT6260501C00240000"


def test_option_canonical_symbol_put():
    assert resolve("NVDA260501P240").to_schwab_symbol() == "NVDA  260501P00240000"


def test_option_canonical_symbol_fractional_strike():
    assert resolve("NVDA260501C202.5").to_schwab_symbol() == "NVDA  260501C00202500"


def test_all_four_forms_round_trip_to_same_schwab_symbol():
    canonical = "NVDA  260501C00240000"
    for raw in [
        "NVDA260501C240",
        "NVDA  260501C240",
        "NVDA260501C240.0",
        "NVDA260501C00240000",
    ]:
        assert resolve(raw).to_schwab_symbol() == canonical, raw


# ---- errors -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "NVDA260501",        # missing C/P + strike
        "NVDA260501X240",    # wrong put/call letter
        "NVDA26C240",        # bad date format
        "1234",              # not a ticker
        "NVDA-260501C240",   # wrong separator
    ],
)
def test_resolve_invalid_raises(raw):
    with pytest.raises(TickerError):
        resolve(raw)
