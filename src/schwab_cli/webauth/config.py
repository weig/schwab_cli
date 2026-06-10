"""Provider configuration: ``~/.config/schwab_cli/webauth/*.json``.

One file per provider. Loading NEVER raises and never blocks startup:
invalid files (bad JSON, http issuer, mixed ``*``, duplicate issuer, …)
are collected into :class:`LoadedProviders.errors` so ``schwab doctor``
can render an ✗ per file while the valid providers keep serving.

``allowed_subjects`` is the single authorization field:

* list form  — ``["sub-or-email", ...]``: allowlist only; scopes come
  from the token.
* dict form  — ``{"sub-or-email": ["scope", ...]}``: allowlist plus
  statically granted scopes (unioned with token scopes; an empty list
  means allowed with token scopes only). This is how providers that
  cannot mint custom scopes (Google ID tokens) get authorized.
* ``"*"``    — accept every authenticated subject. Must be the ONLY
  entry; mixing ``*`` with named entries marks the file invalid.

Default closed: an absent/empty ``allowed_subjects`` loads fine but
matches nobody.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from schwab_cli.paths import config_dir

# Asymmetric signature algorithms only — "none" and HS* (shared-secret)
# are structurally rejected: a leaked config must never enable forgery.
ALLOWED_ALGORITHMS = frozenset({
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
})

_DEFAULT_CLOCK_SKEW_S = 60
# Ceiling on the accepted skew: a typo'd 3600 would silently let
# hour-old (replayed) tokens verify.
_MAX_CLOCK_SKEW_S = 300


class WebAuthConfigError(ValueError):
    """A single provider file failed validation (collected, not raised
    out of :func:`load_providers`)."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256",)
    jwks_uri: str | None = None
    scope_claim: str = "scope"
    allow_all_subjects: bool = False
    subject_scopes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    wildcard_scopes: frozenset[str] = frozenset()
    clock_skew_s: int = _DEFAULT_CLOCK_SKEW_S


@dataclass(frozen=True)
class ProviderError:
    path: str
    reason: str


@dataclass(frozen=True)
class LoadedProviders:
    providers: tuple[ProviderConfig, ...]
    errors: tuple[ProviderError, ...]
    disabled: tuple[str, ...] = ()


def webauth_dir() -> Path:
    return config_dir() / "webauth"


def load_providers(directory: Path | None = None) -> LoadedProviders:
    """Load every ``*.json`` under ``directory`` (default: the config
    dir). Never raises; see module docstring for the error contract."""
    d = webauth_dir() if directory is None else directory
    if not d.is_dir():
        return LoadedProviders(providers=(), errors=())

    providers: list[ProviderConfig] = []
    errors: list[ProviderError] = []
    disabled: list[str] = []
    paths: dict[str, str] = {}  # provider name -> file path (for dup reporting)

    for path in sorted(d.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            errors.append(ProviderError(str(path), f"malformed JSON: {e}"))
            continue
        if not isinstance(raw, dict):
            errors.append(ProviderError(str(path), "expected a JSON object"))
            continue
        if raw.get("enabled", True) is False:
            disabled.append(path.stem)
            continue
        try:
            provider = _parse_provider(raw, default_name=path.stem)
        except WebAuthConfigError as e:
            errors.append(ProviderError(str(path), str(e)))
            continue
        providers.append(provider)
        paths[provider.name] = str(path)

    # Duplicate issuers: ALL involved providers are invalidated (we cannot
    # know which one the operator meant), but startup proceeds.
    by_issuer: dict[str, list[ProviderConfig]] = {}
    for p in providers:
        by_issuer.setdefault(p.issuer, []).append(p)
    surviving: list[ProviderConfig] = []
    for issuer, group in by_issuer.items():
        if len(group) == 1:
            surviving.append(group[0])
            continue
        names = ", ".join(sorted(p.name for p in group))
        for p in group:
            errors.append(ProviderError(
                paths[p.name],
                f"duplicate issuer {issuer!r} (also in: {names}) — "
                "all providers with this issuer are disabled",
            ))

    surviving.sort(key=lambda p: p.name)
    return LoadedProviders(
        providers=tuple(surviving),
        errors=tuple(errors),
        disabled=tuple(disabled),
    )


def _parse_provider(raw: dict, *, default_name: str) -> ProviderConfig:
    issuer = raw.get("issuer")
    if not isinstance(issuer, str) or not issuer.startswith("https://"):
        raise WebAuthConfigError("issuer must be an https:// URL")
    parsed = urlparse(issuer)
    if parsed.scheme != "https" or not parsed.netloc or "@" in parsed.netloc:
        raise WebAuthConfigError(
            "issuer must be a plain https:// URL with a hostname "
            "(no userinfo)"
        )

    audience = raw.get("audience")
    if not isinstance(audience, str) or not audience:
        raise WebAuthConfigError("audience is required and must be non-empty")

    algorithms = raw.get("algorithms", ["RS256"])
    if (
        not isinstance(algorithms, list)
        or not algorithms
        or not all(isinstance(a, str) for a in algorithms)
    ):
        raise WebAuthConfigError("algorithms must be a non-empty list of strings")
    bad = [a for a in algorithms if a not in ALLOWED_ALGORITHMS]
    if bad:
        raise WebAuthConfigError(
            f"unsupported algorithm(s) {bad} — only asymmetric "
            f"{sorted(ALLOWED_ALGORITHMS)} are accepted"
        )

    jwks_uri = raw.get("jwks_uri")
    if jwks_uri is not None and (
        not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://")
    ):
        raise WebAuthConfigError("jwks_uri must be an https:// URL when set")

    scope_claim = raw.get("scope_claim", "scope")
    if not isinstance(scope_claim, str) or not scope_claim:
        raise WebAuthConfigError("scope_claim must be a non-empty string")

    clock_skew_s = raw.get("clock_skew_s", _DEFAULT_CLOCK_SKEW_S)
    if (
        isinstance(clock_skew_s, bool)  # bool IS int in Python — reject
        or not isinstance(clock_skew_s, int)
        or not 0 <= clock_skew_s <= _MAX_CLOCK_SKEW_S
    ):
        raise WebAuthConfigError(
            f"clock_skew_s must be an integer between 0 and {_MAX_CLOCK_SKEW_S}"
        )

    allow_all, subject_scopes, wildcard_scopes = _parse_allowed_subjects(
        raw.get("allowed_subjects", []),
    )

    name = raw.get("name", default_name)
    if not isinstance(name, str) or not name:
        raise WebAuthConfigError("name must be a non-empty string")

    return ProviderConfig(
        name=name,
        issuer=issuer,
        audience=audience,
        algorithms=tuple(algorithms),
        jwks_uri=jwks_uri,
        scope_claim=scope_claim,
        allow_all_subjects=allow_all,
        subject_scopes=subject_scopes,
        wildcard_scopes=wildcard_scopes,
        clock_skew_s=clock_skew_s,
    )


def _parse_allowed_subjects(
    raw: object,
) -> tuple[bool, dict[str, frozenset[str]], frozenset[str]]:
    """Normalize both forms; returns (allow_all, subject→static, wildcard-static)."""
    if isinstance(raw, list):
        if not all(isinstance(s, str) and s for s in raw):
            raise WebAuthConfigError(
                "allowed_subjects list entries must be non-empty strings"
            )
        entries: dict[str, frozenset[str]] = {s: frozenset() for s in raw}
    elif isinstance(raw, dict):
        entries = {}
        for subject, scopes in raw.items():
            if not isinstance(subject, str) or not subject:
                raise WebAuthConfigError(
                    "allowed_subjects keys must be non-empty strings"
                )
            if (
                not isinstance(scopes, list)
                or not all(isinstance(s, str) and s for s in scopes)
            ):
                raise WebAuthConfigError(
                    f"allowed_subjects[{subject!r}] must be a list of scopes"
                )
            entries[subject] = frozenset(scopes)
    else:
        raise WebAuthConfigError(
            "allowed_subjects must be a list or an object"
        )

    if "*" in entries:
        if len(entries) > 1:
            raise WebAuthConfigError(
                "'*' must be the ONLY allowed_subjects entry — mixing the "
                "wildcard with named subjects is ambiguous and rejected"
            )
        return True, {}, entries["*"]
    return False, entries, frozenset()
