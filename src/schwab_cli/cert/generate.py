"""Pure certificate generation via the ``cryptography`` library.

Generates a self-signed, name-constrained local CA (limited to 127.0.0.1)
and leaf certificates signed by that CA for the loopback HTTPS callback
server. All time handling is timezone-aware UTC and injectable for
deterministic tests.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_COMMON_NAME = "Schwab CLI Local CA"
LEAF_COMMON_NAME = "127.0.0.1"
DEFAULT_VALID_DAYS = 3650
RSA_KEY_SIZE = 2048
RSA_PUBLIC_EXPONENT = 65537


@dataclass(frozen=True)
class CertKeyPair:
    """A certificate paired with its private key."""

    cert: x509.Certificate
    key: RSAPrivateKey


def _utc_now() -> datetime:
    # X.509 validity fields are encoded at whole-second resolution (the
    # library silently truncates sub-second precision). Truncate the current
    # instant to whole seconds so the value we set matches the value read
    # back, without advancing into the future.
    return datetime.now(timezone.utc).replace(microsecond=0)


def _new_rsa_key() -> RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=RSA_PUBLIC_EXPONENT,
        key_size=RSA_KEY_SIZE,
    )


def generate_ca(
    *, now: datetime | None = None, valid_days: int = DEFAULT_VALID_DAYS
) -> CertKeyPair:
    """Generate a self-signed, name-constrained local CA.

    The CA is constrained (critical NameConstraints) to permit only the
    loopback address 127.0.0.1/32, so even if its private key leaked it
    could not mint trusted certs for any public host.
    """
    if now is None:
        now = _utc_now()

    key = _new_rsa_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    loopback_network = ipaddress.IPv4Network("127.0.0.1/32")

    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.IPAddress(loopback_network)],
                excluded_subtrees=None,
            ),
            critical=True,
        )
    )

    cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
    return CertKeyPair(cert=cert, key=key)


def generate_leaf(
    ca: CertKeyPair,
    *,
    host: str = LEAF_COMMON_NAME,
    now: datetime | None = None,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> CertKeyPair:
    """Generate a leaf server certificate signed by ``ca``."""
    if now is None:
        now = _utc_now()

    key = _new_rsa_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(host))]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )

    cert = builder.sign(private_key=ca.key, algorithm=hashes.SHA256())
    return CertKeyPair(cert=cert, key=key)


def _format_fingerprint(digest: bytes) -> str:
    return ":".join(f"{b:02X}" for b in digest)


def sha256_fingerprint(cert: x509.Certificate) -> str:
    """Return the SHA-256 fingerprint as uppercase colon-delimited hex pairs.

    Display form only. For the macOS ``security`` CLI use
    :func:`sha256_fingerprint_hex` (no colons).
    """
    return _format_fingerprint(cert.fingerprint(hashes.SHA256()))


def sha256_fingerprint_hex(cert: x509.Certificate) -> str:
    """Return the SHA-256 fingerprint as plain uppercase hex (NO colons).

    This is the exact form the macOS ``security delete-certificate -Z``
    flag accepts. The hash here is used purely as a non-cryptographic
    certificate identifier for the CLI, not for any security decision.
    """
    return cert.fingerprint(hashes.SHA256()).hex().upper()


def cert_to_pem(cert: x509.Certificate) -> bytes:
    """Serialise a certificate to PEM bytes."""
    return cert.public_bytes(Encoding.PEM)


def key_to_pem(key: RSAPrivateKey) -> bytes:
    """Serialise a private key to unencrypted PKCS8 PEM bytes."""
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
