"""Tests for `schwab_cli.redirect_uri`.

Tests ``parse_callback_uri`` and ``is_loopback_https`` from the new
``redirect_uri`` module. These run before the module is implemented so
they should fail with ``ModuleNotFoundError`` (RED phase).
"""
from __future__ import annotations

import pytest

from schwab_cli.redirect_uri import CallbackTarget, is_loopback_https, parse_callback_uri


# ---------------------------------------------------------------------
# parse_callback_uri — happy-path parsing
# ---------------------------------------------------------------------


def test_parse_full_uri_with_explicit_port():
    """Canonical production URI: explicit port + non-trivial path."""
    result = parse_callback_uri("https://127.0.0.1:19806/schwab/callback")
    assert result == CallbackTarget(
        scheme="https",
        host="127.0.0.1",
        port=19806,
        path="/schwab/callback",
    )


def test_parse_https_default_port_when_omitted():
    """No port in URI → default HTTPS port 443."""
    result = parse_callback_uri("https://127.0.0.1/cb")
    assert result.port == 443
    assert result.scheme == "https"
    assert result.host == "127.0.0.1"
    assert result.path == "/cb"


def test_parse_explicit_port_443_is_preserved():
    """Explicitly specified 443 is the same as the default but must not
    be altered."""
    result = parse_callback_uri("https://127.0.0.1:443/callback")
    assert result.port == 443


def test_parse_high_port_respected():
    result = parse_callback_uri("https://127.0.0.1:65000/x")
    assert result.port == 65000


def test_parse_path_defaults_to_slash_when_empty():
    """A URI with no path component should produce path '/'."""
    result = parse_callback_uri("https://127.0.0.1:19806")
    assert result.path == "/"


def test_parse_localhost_host():
    result = parse_callback_uri("https://localhost:19806/schwab/callback")
    assert result.host == "localhost"
    assert result.port == 19806


def test_parse_ipv6_loopback():
    result = parse_callback_uri("https://[::1]:19806/path")
    assert result.host == "::1"
    assert result.port == 19806
    assert result.path == "/path"


def test_parse_http_scheme():
    """http scheme is test-only but must parse correctly."""
    result = parse_callback_uri("http://127.0.0.1:8080/cb")
    assert result.scheme == "http"
    assert result.port == 8080


def test_parse_http_default_port():
    """http default port is 80."""
    result = parse_callback_uri("http://127.0.0.1/cb")
    assert result.port == 80


def test_parse_returns_frozen_dataclass():
    """CallbackTarget must be frozen — mutation should raise."""
    result = parse_callback_uri("https://127.0.0.1:19806/schwab/callback")
    with pytest.raises((AttributeError, TypeError)):
        result.port = 9999  # type: ignore[misc]


def test_parse_callback_target_is_dataclass_with_expected_fields():
    result = parse_callback_uri("https://127.0.0.1:19806/schwab/callback")
    # Access all four fields — structural check.
    assert result.scheme == "https"
    assert result.host == "127.0.0.1"
    assert result.port == 19806
    assert result.path == "/schwab/callback"


def test_parse_external_relay_uri():
    """Non-loopback URIs must also parse (used in is_loopback_https False checks)."""
    result = parse_callback_uri("https://relay.example.com/x")
    assert result.host == "relay.example.com"
    assert result.scheme == "https"
    assert result.port == 443


# ---------------------------------------------------------------------
# is_loopback_https
# ---------------------------------------------------------------------


def test_is_loopback_https_true_for_127_with_port():
    assert is_loopback_https("https://127.0.0.1:19806/schwab/callback") is True


def test_is_loopback_https_true_for_127_no_port():
    assert is_loopback_https("https://127.0.0.1/cb") is True


def test_is_loopback_https_true_for_localhost():
    assert is_loopback_https("https://localhost:19806/cb") is True


def test_is_loopback_https_true_for_ipv6_loopback():
    assert is_loopback_https("https://[::1]:19806/path") is True


def test_is_loopback_https_false_for_http_127():
    """http scheme → False even if host is loopback."""
    assert is_loopback_https("http://127.0.0.1:19806/cb") is False


def test_is_loopback_https_false_for_external_host():
    assert is_loopback_https("https://relay.example.com/x") is False


def test_is_loopback_https_false_for_http_localhost():
    assert is_loopback_https("http://localhost:19806/cb") is False


def test_is_loopback_https_false_for_non_loopback_https():
    """A public host over https is NOT a loopback https URI."""
    assert is_loopback_https("https://192.168.1.1:19806/cb") is False
