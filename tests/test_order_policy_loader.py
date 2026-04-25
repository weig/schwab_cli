"""Loader tests — per-file profiles.

Phase 2f drops inheritance + the bundled reserved-profile fallback.
The loader is now: read JSON → parse → return.
"""

from __future__ import annotations

import json

import pytest

from schwab_cli.order_policy import (
    PolicyConfigError,
    list_profiles,
    load_profile,
)
from schwab_cli.order_policy.loader import select_profile_name


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


def test_inherit_field_in_legacy_profile_is_rejected(tmp_path):
    _write(tmp_path, "x", {
        "inherit": "base",
        "default_action": "allow",
    })
    with pytest.raises(PolicyConfigError, match="inheritance was dropped"):
        load_profile("x", base_dir=tmp_path)


def test_legacy_override_fields_rejected_with_pointer(tmp_path):
    _write(tmp_path, "x", {
        "default_action": "deny",
        "allow_override": True,
        "policies": [],
    })
    with pytest.raises(PolicyConfigError, match="per-profile override gating"):
        load_profile("x", base_dir=tmp_path)


def test_unknown_top_level_key_rejected(tmp_path):
    _write(tmp_path, "x", {
        "default_action": "deny",
        "spurious_field": True,
        "policies": [],
    })
    with pytest.raises(PolicyConfigError, match="unknown profile field"):
        load_profile("x", base_dir=tmp_path)


def test_missing_default_helpful_error(tmp_path):
    """First-run UX: no profiles dir, no flag, no env → helpful pointer."""
    with pytest.raises(PolicyConfigError, match="profile new"):
        load_profile("default", base_dir=tmp_path)


def test_missing_named_profile_lists_available(tmp_path):
    _write(tmp_path, "wheel_prod", {
        "default_action": "deny", "policies": [],
    })
    with pytest.raises(PolicyConfigError, match="not found"):
        load_profile("does_not_exist", base_dir=tmp_path)


def test_invalid_profile_name_rejected(tmp_path):
    with pytest.raises(PolicyConfigError, match="invalid profile name"):
        load_profile("../escape", base_dir=tmp_path)


def test_list_profiles_empty_when_no_files(tmp_path):
    assert list_profiles(base_dir=tmp_path) == []


def test_list_profiles_returns_user_files_sorted(tmp_path):
    _write(tmp_path, "wheel_prod", {"default_action": "deny", "policies": []})
    _write(tmp_path, "ko_test", {"default_action": "deny", "policies": []})
    names = list_profiles(base_dir=tmp_path)
    assert names == ["ko_test", "wheel_prod"]


def test_select_profile_name_priority_flag_first():
    assert select_profile_name(flag="x", env="y") == "x"


def test_select_profile_name_env_when_no_flag():
    assert select_profile_name(flag=None, env="y") == "y"


def test_select_profile_name_default_when_neither():
    assert select_profile_name(flag=None, env=None) == "default"
