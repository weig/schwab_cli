import pytest

from schwab_cli.output.format import Format, FormatError, pick_format


def test_default_is_human():
    assert pick_format(False, False) is Format.HUMAN


def test_json_flag_picks_json():
    assert pick_format(True, False) is Format.JSON


def test_md_flag_picks_md():
    assert pick_format(False, True) is Format.MD


def test_both_flags_raise():
    with pytest.raises(FormatError, match="mutually exclusive"):
        pick_format(True, True)


def test_format_enum_has_three_variants():
    assert {f.name for f in Format} == {"HUMAN", "JSON", "MD"}
