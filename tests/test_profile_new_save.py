"""Atomic save tests for profile_new.save."""

from __future__ import annotations

import json
import stat

import pytest

from schwab_cli.order_policy.profile_new.save import (
    ProfileExistsError, atomic_save,
)


def test_atomic_save_writes_file_with_0600(tmp_path):
    base = tmp_path / "profiles" / "order"
    p = atomic_save(
        profile_name="my_prof",
        profile_data={
            "description": "x",
            "default_action": "deny",
            "policies": [],
        },
        base_dir=base,
    )
    assert p == base / "my_prof.json"
    raw = json.loads(p.read_text())
    assert raw["default_action"] == "deny"
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_atomic_save_refuses_to_overwrite_existing(tmp_path):
    base = tmp_path / "profiles" / "order"
    atomic_save(
        profile_name="x",
        profile_data={"default_action": "allow", "policies": []},
        base_dir=base,
    )
    with pytest.raises(ProfileExistsError):
        atomic_save(
            profile_name="x",
            profile_data={"default_action": "deny", "policies": []},
            base_dir=base,
        )


def test_atomic_save_no_leftover_temp_file(tmp_path):
    base = tmp_path / "profiles" / "order"
    atomic_save(
        profile_name="x",
        profile_data={"default_action": "allow", "policies": []},
        base_dir=base,
    )
    leftovers = list(base.glob("*.tmp"))
    assert leftovers == []


def test_atomic_save_creates_directory(tmp_path):
    deep = tmp_path / "deep" / "profiles" / "order"
    p = atomic_save(
        profile_name="x",
        profile_data={"default_action": "allow", "policies": []},
        base_dir=deep,
    )
    assert deep.exists()
    assert p.exists()
