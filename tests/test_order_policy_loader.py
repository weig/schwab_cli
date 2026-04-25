"""Loader tests — per-file profiles, inheritance, reserved fallback."""

from __future__ import annotations

import json

import pytest

from schwab_cli.order_policy import (
    PolicyConfigError,
    list_profiles,
    load_profile,
)
from schwab_cli.order_policy.loader import RESERVED_PROFILES, select_profile_name


def _write(base, name, body):
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.json").write_text(json.dumps(body))


def test_loads_simple_profile_from_disk(tmp_path):
    _write(tmp_path, "default", {
        "default_action": "deny",
        "policies": [{"name": "p", "effect": "allow", "match": "*"}],
    })
    p = load_profile("default", base_dir=tmp_path)
    assert p.name == "default"
    assert len(p.policies) == 1


def test_inherit_chain_two_levels(tmp_path):
    _write(tmp_path, "base", {
        "default_action": "deny",
        "policies": [{"name": "base_rule", "effect": "deny", "match": "*"}],
    })
    _write(tmp_path, "child", {
        "inherit": "base",
        "overrides": {"default_action": "allow"},
        "policies": [{"name": "child_rule", "effect": "allow", "match": "*"}],
    })
    p = load_profile("child", base_dir=tmp_path)
    assert p.default_action == "allow"
    # Child policies are appended after parent's.
    assert [pp.name for pp in p.policies] == ["base_rule", "child_rule"]


def test_inherit_cycle_detected(tmp_path):
    _write(tmp_path, "a", {"inherit": "b", "default_action": "allow"})
    _write(tmp_path, "b", {"inherit": "a", "default_action": "allow"})
    with pytest.raises(PolicyConfigError, match="cycle"):
        load_profile("a", base_dir=tmp_path)


def test_reserved_profile_falls_back_to_bundled_when_no_user_file(tmp_path):
    p = load_profile("emergency_stop", base_dir=tmp_path)
    assert p.name == "emergency_stop"
    assert p.default_action == "deny"
    assert p.allow_override is False


def test_user_file_overrides_bundled_reserved(tmp_path):
    _write(tmp_path, "default", {
        "description": "user-customised default",
        "default_action": "deny",
        "policies": [],
    })
    p = load_profile("default", base_dir=tmp_path)
    assert "user-customised" in p.description
    assert p.default_action == "deny"


def test_missing_profile_raises_with_helpful_listing(tmp_path):
    with pytest.raises(PolicyConfigError, match="not found"):
        load_profile("does_not_exist", base_dir=tmp_path)


def test_invalid_profile_name_rejected(tmp_path):
    with pytest.raises(PolicyConfigError, match="invalid profile name"):
        load_profile("../escape", base_dir=tmp_path)


def test_list_profiles_includes_reserved_even_when_empty(tmp_path):
    names = list_profiles(base_dir=tmp_path)
    assert set(RESERVED_PROFILES).issubset(set(names))


def test_list_profiles_merges_user_and_reserved(tmp_path):
    _write(tmp_path, "wheel_prod", {
        "default_action": "deny", "policies": [],
    })
    names = list_profiles(base_dir=tmp_path)
    assert "wheel_prod" in names
    assert "default" in names      # reserved fallback
    assert "emergency_stop" in names


def test_select_profile_name_priority_flag_first():
    assert select_profile_name(flag="x", env="y") == "x"


def test_select_profile_name_env_when_no_flag():
    assert select_profile_name(flag=None, env="y") == "y"


def test_select_profile_name_default_when_neither():
    assert select_profile_name(flag=None, env=None) == "default"
