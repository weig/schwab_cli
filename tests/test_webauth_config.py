"""Spec tests for schwab_cli.webauth.config — provider file loading.

Layout: ~/.config/schwab_cli/webauth/<name>.json, one provider per file.
Invalid files NEVER block startup: they land in ``errors`` (surfaced by
`schwab doctor` with an ✗) while valid providers keep working.
"""
from __future__ import annotations

import json

import pytest

from schwab_cli.webauth.config import load_providers


@pytest.fixture
def webauth_dir(tmp_path):
    d = tmp_path / "webauth"
    d.mkdir()
    return d


def _write(d, name: str, payload: dict | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (d / f"{name}.json").write_text(text)


def _minimal(**over) -> dict:
    base = {
        "issuer": "https://tenant.us.auth0.com/",
        "audience": "https://schwab-api.local",
        "allowed_subjects": ["auth0|abc"],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Happy path + defaults
# ---------------------------------------------------------------------------


def test_loads_minimal_provider_with_defaults(webauth_dir):
    _write(webauth_dir, "auth0", _minimal())
    loaded = load_providers(webauth_dir)
    assert loaded.errors == ()
    (p,) = loaded.providers
    assert p.name == "auth0"  # filename stem is the default name
    assert p.issuer == "https://tenant.us.auth0.com/"
    assert p.audience == "https://schwab-api.local"
    assert p.algorithms == ("RS256",)
    assert p.scope_claim == "scope"
    assert p.clock_skew_s == 60
    assert p.jwks_uri is None
    assert p.allow_all_subjects is False
    assert p.subject_scopes == {"auth0|abc": frozenset()}


def test_explicit_name_overrides_filename(webauth_dir):
    _write(webauth_dir, "file-a", _minimal(name="primary"))
    loaded = load_providers(webauth_dir)
    assert loaded.providers[0].name == "primary"


def test_missing_dir_is_empty_not_error(tmp_path):
    loaded = load_providers(tmp_path / "nope")
    assert loaded.providers == ()
    assert loaded.errors == ()


def test_dict_allowed_subjects_carries_static_scopes(webauth_dir):
    _write(webauth_dir, "g", _minimal(
        issuer="https://accounts.google.com",
        allowed_subjects={
            "me@gmail.com": ["marketdata", "streaming"],
            "auth0|xyz": [],
        },
    ))
    loaded = load_providers(webauth_dir)
    (p,) = loaded.providers
    assert p.subject_scopes["me@gmail.com"] == frozenset({"marketdata", "streaming"})
    assert p.subject_scopes["auth0|xyz"] == frozenset()


def test_wildcard_alone_allows_everyone(webauth_dir):
    _write(webauth_dir, "open", _minimal(allowed_subjects=["*"]))
    loaded = load_providers(webauth_dir)
    (p,) = loaded.providers
    assert p.allow_all_subjects is True
    assert p.wildcard_scopes == frozenset()


def test_wildcard_dict_grants_static_scopes_to_everyone(webauth_dir):
    _write(webauth_dir, "open", _minimal(
        allowed_subjects={"*": ["marketdata"]},
    ))
    (p,) = load_providers(webauth_dir).providers
    assert p.allow_all_subjects is True
    assert p.wildcard_scopes == frozenset({"marketdata"})


def test_disabled_provider_skipped(webauth_dir):
    _write(webauth_dir, "off", _minimal(enabled=False))
    loaded = load_providers(webauth_dir)
    assert loaded.providers == ()
    assert loaded.errors == ()
    assert loaded.disabled == ("off",)


# ---------------------------------------------------------------------------
# Invalid files: collected as errors, never raise
# ---------------------------------------------------------------------------


def _assert_single_error(loaded, fragment: str):
    assert loaded.providers == ()
    assert len(loaded.errors) == 1
    assert fragment in loaded.errors[0].reason


def test_malformed_json_is_an_error(webauth_dir):
    _write(webauth_dir, "bad", "{not json")
    _assert_single_error(load_providers(webauth_dir), "JSON")


def test_http_issuer_rejected(webauth_dir):
    _write(webauth_dir, "bad", _minimal(issuer="http://insecure.example"))
    _assert_single_error(load_providers(webauth_dir), "https")


def test_missing_audience_rejected(webauth_dir):
    cfg = _minimal()
    del cfg["audience"]
    _write(webauth_dir, "bad", cfg)
    _assert_single_error(load_providers(webauth_dir), "audience")


def test_symmetric_and_none_algorithms_rejected(webauth_dir):
    _write(webauth_dir, "bad", _minimal(algorithms=["HS256"]))
    _assert_single_error(load_providers(webauth_dir), "algorithm")


def test_wildcard_mixed_with_named_subjects_rejected(webauth_dir):
    _write(webauth_dir, "bad", _minimal(allowed_subjects=["*", "me@x.com"]))
    _assert_single_error(load_providers(webauth_dir), "*")


def test_wildcard_mixed_in_dict_rejected(webauth_dir):
    _write(webauth_dir, "bad", _minimal(
        allowed_subjects={"*": [], "me@x.com": ["accounts"]},
    ))
    _assert_single_error(load_providers(webauth_dir), "*")


def test_duplicate_issuer_invalidates_both_but_loads_rest(webauth_dir):
    _write(webauth_dir, "a", _minimal())
    _write(webauth_dir, "b", _minimal())  # same issuer
    _write(webauth_dir, "c", _minimal(issuer="https://other.example/"))
    loaded = load_providers(webauth_dir)
    assert [p.name for p in loaded.providers] == ["c"]
    assert len(loaded.errors) == 2
    assert all("duplicate issuer" in e.reason for e in loaded.errors)


def test_one_bad_file_does_not_block_good_ones(webauth_dir):
    _write(webauth_dir, "bad", "{nope")
    _write(webauth_dir, "good", _minimal())
    loaded = load_providers(webauth_dir)
    assert [p.name for p in loaded.providers] == ["good"]
    assert len(loaded.errors) == 1
