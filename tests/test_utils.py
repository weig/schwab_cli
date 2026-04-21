import pytest

from schwab_cli.utils import _is_debug_truthy, _summarize_error


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "Yes", "1"])
def test_is_debug_truthy_accepts_known_truthy(value):
    assert _is_debug_truthy(value) is True


@pytest.mark.parametrize("value", [None, "", "0", "no", "false", "off", "  true  "])
def test_is_debug_truthy_rejects_others(value):
    # Whitespace-padded "true" returns False — env vars should be exact.
    assert _is_debug_truthy(value) is False


def test_summarize_error_format_for_oauth_error():
    from schwab_cli.oauth import OAuthError
    assert _summarize_error(OAuthError("missing field")) == "missing field"


def test_summarize_error_format_for_request_error():
    import httpx
    err = httpx.ConnectError("dns failed")
    assert _summarize_error(err) == "network: ConnectError"


def test_summarize_error_format_for_status_error():
    import httpx
    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(401, request=req, json={"error": "invalid_grant"})
    err = httpx.HTTPStatusError("401", request=req, response=resp)
    summary = _summarize_error(err)
    assert summary.startswith("401 ")
    assert "invalid_grant" in summary
