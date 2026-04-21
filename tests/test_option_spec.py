from datetime import date

import pytest

from schwab_cli.option_spec import OptionSpec, OptionSpecError, parse_option_spec


_TODAY = date(2026, 4, 21)


def test_date_only_means_all_types_no_strike():
    spec = parse_option_spec("270115", today=_TODAY)
    assert spec == OptionSpec(
        expiry=date(2027, 1, 15), contract_type="ALL", strike=None
    )


def test_date_star_equivalent_to_date_only():
    assert parse_option_spec("270115*", today=_TODAY) == parse_option_spec(
        "270115", today=_TODAY
    )


def test_date_put_no_star():
    spec = parse_option_spec("270115P", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike is None


def test_date_put_star():
    spec = parse_option_spec("270115P*", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike is None


def test_date_call_star():
    spec = parse_option_spec("270115C*", today=_TODAY)
    assert spec.contract_type == "CALL"
    assert spec.strike is None


def test_date_star_strike_both_types():
    spec = parse_option_spec("270115*250", today=_TODAY)
    assert spec.contract_type == "ALL"
    assert spec.strike == 250.0


def test_date_put_star_strike():
    spec = parse_option_spec("270115P*250", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike == 250.0


def test_date_call_star_strike():
    spec = parse_option_spec("270115C*250", today=_TODAY)
    assert spec.contract_type == "CALL"
    assert spec.strike == 250.0


def test_decimal_strike():
    spec = parse_option_spec("270115*250.5", today=_TODAY)
    assert spec.strike == 250.5


def test_empty_string_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("", today=_TODAY)


def test_short_date_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("27015", today=_TODAY)


def test_bad_type_letter_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115X*250", today=_TODAY)


def test_non_numeric_strike_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115*abc", today=_TODAY)


def test_past_expiry_rejected():
    with pytest.raises(OptionSpecError) as exc:
        parse_option_spec("200115", today=_TODAY)
    assert "past" in str(exc.value).lower()


def test_same_day_expiry_allowed():
    # 2026-04-21 today, 260421 expiry — same-day is still live.
    spec = parse_option_spec("260421", today=_TODAY)
    assert spec.expiry == _TODAY


def test_impossible_date_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270230", today=_TODAY)  # Feb 30


def test_strike_without_star_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115250", today=_TODAY)


def test_typed_strike_without_star_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115P250", today=_TODAY)


def test_past_expiry_error_kind_is_expired():
    with pytest.raises(OptionSpecError) as exc:
        parse_option_spec("200115", today=_TODAY)
    assert exc.value.kind == "expired"


def test_bad_date_error_kind_is_bad_date():
    with pytest.raises(OptionSpecError) as exc:
        parse_option_spec("270230", today=_TODAY)
    assert exc.value.kind == "bad_date"


def test_grammar_miss_error_kind_is_invalid():
    with pytest.raises(OptionSpecError) as exc:
        parse_option_spec("abcdef", today=_TODAY)
    assert exc.value.kind == "invalid"
