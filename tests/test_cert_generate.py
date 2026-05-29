"""Unit tests for schwab_cli.cert.generate (RED phase — no implementation yet).

API CONTRACT settled by these tests
====================================
Module: ``schwab_cli.cert.generate``

``CertKeyPair``
    Dataclass with fields:
        cert : cryptography.x509.Certificate
        key  : cryptography RSA private key object

``generate_ca(*, now: datetime | None = None, valid_days: int = 3650) -> CertKeyPair``
    Generates a self-signed name-constrained local CA.
    - Subject / Issuer CN = "Schwab CLI Local CA"
    - BasicConstraints: CA=True
    - KeyUsage: key_cert_sign=True, crl_sign=True (digital_signature implied)
    - ExtendedKeyUsage: NOT required for CA; omitted or OCSPSigning only.
    - NameConstraints: permitted subtrees include ONLY IPAddress(127.0.0.1/32);
      no excluded subtrees.
    - not_valid_before == now (default: utcnow at call time)
    - not_valid_after  == now + timedelta(days=valid_days)

``generate_leaf(ca: CertKeyPair, *, host: str = "127.0.0.1",
                now: datetime | None = None, valid_days: int = 3650) -> CertKeyPair``
    Generates a leaf cert signed by the given CA.
    - SubjectAlternativeName: IPAddress(127.0.0.1) when host="127.0.0.1"
    - ExtendedKeyUsage: SERVER_AUTH
    - BasicConstraints: CA=False
    - Issuer == CA subject (CN="Schwab CLI Local CA")
    - Signature verifiable against CA public key

``sha256_fingerprint(cert: x509.Certificate) -> str``
    Returns hex string of SHA-256 fingerprint, uppercase, colon-delimited pairs.
    e.g. "AB:CD:EF:..."

``sha256_fingerprint_hex(cert: x509.Certificate) -> str``
    Returns the SHA-256 fingerprint as plain uppercase hex with NO colons.
    This is the form the macOS ``security ... -Z`` flag accepts; used as a
    non-cryptographic CLI identifier only. e.g. "ABCDEF01...".

``cert_to_pem(cert: x509.Certificate) -> bytes``
    Returns PEM-encoded certificate bytes (starts with b"-----BEGIN CERTIFICATE-----").

``key_to_pem(key) -> bytes``
    Returns PEM-encoded private key bytes (starts with b"-----BEGIN").

``now`` injection:
    When ``now`` is provided as a timezone-aware UTC datetime, ``not_valid_before``
    and ``not_valid_after`` are derived from it deterministically (no wall-clock).
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

import pytest

from schwab_cli.cert.generate import (
    CertKeyPair,
    cert_to_pem,
    generate_ca,
    generate_leaf,
    key_to_pem,
    sha256_fingerprint,
    sha256_fingerprint_hex,
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def ca_pair() -> CertKeyPair:
    return generate_ca(now=FIXED_NOW)


@pytest.fixture(scope="module")
def leaf_pair(ca_pair: CertKeyPair) -> CertKeyPair:
    return generate_leaf(ca_pair, now=FIXED_NOW)


# ---------------------------------------------------------------------------
# CertKeyPair dataclass structure
# ---------------------------------------------------------------------------


def test_cert_key_pair_has_cert_and_key_fields(ca_pair: CertKeyPair):
    assert hasattr(ca_pair, "cert")
    assert hasattr(ca_pair, "key")


def test_cert_key_pair_cert_is_x509_certificate(ca_pair: CertKeyPair):
    from cryptography import x509

    assert isinstance(ca_pair.cert, x509.Certificate)


def test_cert_key_pair_key_has_private_bytes_method(ca_pair: CertKeyPair):
    # Any cryptography private-key type exposes private_bytes().
    assert callable(getattr(ca_pair.key, "private_bytes", None))


# ---------------------------------------------------------------------------
# generate_ca — subject / issuer
# ---------------------------------------------------------------------------


def test_ca_subject_cn_is_schwab_cli_local_ca(ca_pair: CertKeyPair):
    from cryptography.x509.oid import NameOID

    cn = ca_pair.cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "Schwab CLI Local CA"


def test_ca_issuer_equals_subject(ca_pair: CertKeyPair):
    assert ca_pair.cert.issuer == ca_pair.cert.subject


# ---------------------------------------------------------------------------
# generate_ca — BasicConstraints
# ---------------------------------------------------------------------------


def test_ca_has_basic_constraints_extension(ca_pair: CertKeyPair):
    from cryptography import x509

    ext = ca_pair.cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert ext is not None


def test_ca_basic_constraints_ca_true(ca_pair: CertKeyPair):
    from cryptography import x509

    bc = ca_pair.cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is True


def test_ca_basic_constraints_is_critical(ca_pair: CertKeyPair):
    from cryptography import x509

    ext = ca_pair.cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert ext.critical is True


# ---------------------------------------------------------------------------
# generate_ca — KeyUsage
# ---------------------------------------------------------------------------


def test_ca_has_key_usage_extension(ca_pair: CertKeyPair):
    from cryptography import x509

    ext = ca_pair.cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ext is not None


def test_ca_key_usage_key_cert_sign_true(ca_pair: CertKeyPair):
    from cryptography import x509

    ku = ca_pair.cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.key_cert_sign is True


def test_ca_key_usage_crl_sign_true(ca_pair: CertKeyPair):
    from cryptography import x509

    ku = ca_pair.cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.crl_sign is True


# ---------------------------------------------------------------------------
# generate_ca — NameConstraints (SECURITY CRUX)
# ---------------------------------------------------------------------------


def test_ca_has_name_constraints_extension(ca_pair: CertKeyPair):
    from cryptography import x509

    ext = ca_pair.cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext is not None


def test_ca_name_constraints_is_critical(ca_pair: CertKeyPair):
    from cryptography import x509

    ext = ca_pair.cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical is True


def test_ca_name_constraints_permitted_contains_loopback_ip(ca_pair: CertKeyPair):
    from cryptography import x509

    nc = ca_pair.cert.extensions.get_extension_for_class(x509.NameConstraints).value
    assert nc.permitted_subtrees is not None
    ip_names = [
        g for g in nc.permitted_subtrees if isinstance(g, x509.IPAddress)
    ]
    assert len(ip_names) >= 1, "NameConstraints must have at least one IPAddress permitted"
    # The permitted network must include 127.0.0.1
    loopback = ipaddress.IPv4Address("127.0.0.1")
    networks = [g.value for g in ip_names]
    assert any(loopback in net for net in networks), (
        f"127.0.0.1 must be inside a permitted IPAddress subtree; got {networks}"
    )


def test_ca_name_constraints_permitted_does_not_include_non_loopback(ca_pair: CertKeyPair):
    """A non-loopback IP (e.g. 8.8.8.8) must NOT be in any permitted IP network."""
    from cryptography import x509

    nc = ca_pair.cert.extensions.get_extension_for_class(x509.NameConstraints).value
    if nc.permitted_subtrees is None:
        return  # already fails the previous test
    ip_names = [g for g in nc.permitted_subtrees if isinstance(g, x509.IPAddress)]
    non_loopback = ipaddress.IPv4Address("8.8.8.8")
    networks = [g.value for g in ip_names]
    assert not any(non_loopback in net for net in networks), (
        f"8.8.8.8 must NOT be permitted; got networks {networks}"
    )


def test_ca_name_constraints_no_excluded_subtrees(ca_pair: CertKeyPair):
    from cryptography import x509

    nc = ca_pair.cert.extensions.get_extension_for_class(x509.NameConstraints).value
    # excluded_subtrees should be None or empty — we only use permitted list
    assert not nc.excluded_subtrees, (
        "CA NameConstraints should not set excluded_subtrees; use permitted only"
    )


# ---------------------------------------------------------------------------
# generate_ca — validity / now injection
# ---------------------------------------------------------------------------


def test_ca_not_valid_before_equals_injected_now():
    pair = generate_ca(now=FIXED_NOW)
    # cryptography returns naive UTC datetimes from not_valid_before_utc or
    # not_valid_before; normalize for comparison.
    nbf = pair.cert.not_valid_before_utc
    assert nbf == FIXED_NOW


def test_ca_not_valid_after_equals_now_plus_valid_days():
    days = 100
    pair = generate_ca(now=FIXED_NOW, valid_days=days)
    expected = FIXED_NOW + timedelta(days=days)
    assert pair.cert.not_valid_after_utc == expected


def test_ca_default_valid_days_is_3650():
    pair = generate_ca(now=FIXED_NOW)
    expected = FIXED_NOW + timedelta(days=3650)
    assert pair.cert.not_valid_after_utc == expected


def test_ca_now_none_uses_approximately_current_time():
    # _utc_now() truncates to whole seconds (microsecond=0) WITHOUT advancing
    # into the future, so not_valid_before may be up to ~1s before `before`.
    # Allow that truncation slack on the lower bound.
    before = datetime.now(timezone.utc)
    pair = generate_ca()
    after = datetime.now(timezone.utc)
    nbf = pair.cert.not_valid_before_utc
    assert before.replace(microsecond=0) <= nbf <= after
    # And it must never advance past the observed wall clock.
    assert nbf <= after


# ---------------------------------------------------------------------------
# generate_leaf — subject / issuer / signing
# ---------------------------------------------------------------------------


def test_leaf_issuer_equals_ca_subject(ca_pair: CertKeyPair, leaf_pair: CertKeyPair):
    assert leaf_pair.cert.issuer == ca_pair.cert.subject


def test_leaf_basic_constraints_ca_false(leaf_pair: CertKeyPair):
    from cryptography import x509

    bc = leaf_pair.cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False


def test_leaf_signature_verifies_against_ca_public_key(
    ca_pair: CertKeyPair, leaf_pair: CertKeyPair
):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    ca_pub = ca_pair.cert.public_key()
    # This raises if the signature is invalid.
    ca_pub.verify(
        leaf_pair.cert.signature,
        leaf_pair.cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


# ---------------------------------------------------------------------------
# generate_leaf — SubjectAlternativeName
# ---------------------------------------------------------------------------


def test_leaf_has_subject_alternative_name(leaf_pair: CertKeyPair):
    from cryptography import x509

    ext = leaf_pair.cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert ext is not None


def test_leaf_san_contains_loopback_ip_address(leaf_pair: CertKeyPair):
    from cryptography import x509

    san = leaf_pair.cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    ip_addrs = san.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("127.0.0.1") in ip_addrs


def test_leaf_san_does_not_contain_non_loopback_ip(leaf_pair: CertKeyPair):
    from cryptography import x509

    san = leaf_pair.cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    ip_addrs = san.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("0.0.0.0") not in ip_addrs
    assert ipaddress.IPv4Address("8.8.8.8") not in ip_addrs


# ---------------------------------------------------------------------------
# generate_leaf — ExtendedKeyUsage
# ---------------------------------------------------------------------------


def test_leaf_has_extended_key_usage(leaf_pair: CertKeyPair):
    from cryptography import x509

    ext = leaf_pair.cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert ext is not None


def test_leaf_extended_key_usage_has_server_auth(leaf_pair: CertKeyPair):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    eku = leaf_pair.cert.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku


# ---------------------------------------------------------------------------
# generate_leaf — validity / now injection
# ---------------------------------------------------------------------------


def test_leaf_not_valid_before_equals_injected_now(ca_pair: CertKeyPair):
    pair = generate_leaf(ca_pair, now=FIXED_NOW)
    assert pair.cert.not_valid_before_utc == FIXED_NOW


def test_leaf_not_valid_after_equals_now_plus_valid_days(ca_pair: CertKeyPair):
    days = 200
    pair = generate_leaf(ca_pair, now=FIXED_NOW, valid_days=days)
    assert pair.cert.not_valid_after_utc == FIXED_NOW + timedelta(days=days)


def test_leaf_default_valid_days_is_3650(ca_pair: CertKeyPair):
    pair = generate_leaf(ca_pair, now=FIXED_NOW)
    assert pair.cert.not_valid_after_utc == FIXED_NOW + timedelta(days=3650)


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------


def test_sha256_fingerprint_returns_string(ca_pair: CertKeyPair):
    fp = sha256_fingerprint(ca_pair.cert)
    assert isinstance(fp, str)


def test_sha256_fingerprint_format_is_colon_delimited_hex_pairs(ca_pair: CertKeyPair):
    fp = sha256_fingerprint(ca_pair.cert)
    parts = fp.split(":")
    # SHA-256 = 32 bytes = 64 hex chars = 32 two-char pairs
    assert len(parts) == 32, f"Expected 32 colon-delimited pairs, got {len(parts)}: {fp}"
    assert all(len(p) == 2 for p in parts), f"Each part must be 2 hex chars: {fp}"
    assert fp == fp.upper(), "Fingerprint must be uppercase"


def test_sha256_fingerprint_deterministic(ca_pair: CertKeyPair):
    assert sha256_fingerprint(ca_pair.cert) == sha256_fingerprint(ca_pair.cert)


def test_sha256_fingerprint_hex_is_plain_uppercase_hex_no_colons(ca_pair: CertKeyPair):
    """The CLI-facing form must be plain uppercase hex with NO colons."""
    fp = sha256_fingerprint_hex(ca_pair.cert)
    assert ":" not in fp, f"hex form must not contain colons: {fp}"
    # SHA-256 = 32 bytes = 64 hex chars
    assert len(fp) == 64, f"Expected 64 hex chars, got {len(fp)}: {fp}"
    assert all(c in "0123456789ABCDEF" for c in fp), f"Non-hex char: {fp}"
    assert fp == fp.upper(), "hex form must be uppercase"


def test_sha256_fingerprint_hex_matches_colon_form_without_colons(ca_pair: CertKeyPair):
    """The plain-hex form is the colon form with separators stripped."""
    colon = sha256_fingerprint(ca_pair.cert)
    plain = sha256_fingerprint_hex(ca_pair.cert)
    assert colon.replace(":", "") == plain


def test_sha256_fingerprint_hex_deterministic(ca_pair: CertKeyPair):
    assert sha256_fingerprint_hex(ca_pair.cert) == sha256_fingerprint_hex(
        ca_pair.cert
    )


def test_ca_and_leaf_have_different_sha256_fingerprints(
    ca_pair: CertKeyPair, leaf_pair: CertKeyPair
):
    assert sha256_fingerprint(ca_pair.cert) != sha256_fingerprint(leaf_pair.cert)


# ---------------------------------------------------------------------------
# PEM serialisation helpers
# ---------------------------------------------------------------------------


def test_cert_to_pem_returns_bytes(ca_pair: CertKeyPair):
    assert isinstance(cert_to_pem(ca_pair.cert), bytes)


def test_cert_to_pem_starts_with_pem_header(ca_pair: CertKeyPair):
    pem = cert_to_pem(ca_pair.cert)
    assert pem.startswith(b"-----BEGIN CERTIFICATE-----")


def test_cert_to_pem_ends_with_pem_footer(ca_pair: CertKeyPair):
    pem = cert_to_pem(ca_pair.cert)
    assert b"-----END CERTIFICATE-----" in pem


def test_key_to_pem_returns_bytes(ca_pair: CertKeyPair):
    assert isinstance(key_to_pem(ca_pair.key), bytes)


def test_key_to_pem_starts_with_begin_header(ca_pair: CertKeyPair):
    pem = key_to_pem(ca_pair.key)
    assert pem.startswith(b"-----BEGIN")


def test_cert_to_pem_round_trips_via_load(ca_pair: CertKeyPair):
    """PEM output can be loaded back to recover the same cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    pem = cert_to_pem(ca_pair.cert)
    loaded = x509.load_pem_x509_certificate(pem)
    assert loaded.fingerprint(__import__("cryptography").hazmat.primitives.hashes.SHA256()) \
        == ca_pair.cert.fingerprint(
            __import__("cryptography").hazmat.primitives.hashes.SHA256()
        )


# ---------------------------------------------------------------------------
# Edge / boundary cases
# ---------------------------------------------------------------------------


def test_generate_ca_valid_days_1_produces_single_day_cert():
    pair = generate_ca(now=FIXED_NOW, valid_days=1)
    assert pair.cert.not_valid_after_utc == FIXED_NOW + timedelta(days=1)


def test_generate_leaf_different_calls_produce_different_serials(ca_pair: CertKeyPair):
    """Each call should generate a unique serial number."""
    p1 = generate_leaf(ca_pair, now=FIXED_NOW)
    p2 = generate_leaf(ca_pair, now=FIXED_NOW)
    assert p1.cert.serial_number != p2.cert.serial_number


def test_generate_ca_different_calls_produce_different_keys():
    """Each CA generation uses a fresh key pair."""
    p1 = generate_ca(now=FIXED_NOW)
    p2 = generate_ca(now=FIXED_NOW)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pub1 = p1.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    pub2 = p2.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    assert pub1 != pub2
