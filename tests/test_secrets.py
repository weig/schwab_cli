import subprocess
from unittest.mock import patch

import pytest

from schwab_cli.secrets import SecretError, resolve_secret


def test_literal_value_returned_verbatim():
    assert resolve_secret("plain-text-password") == "plain-text-password"


def test_empty_value_returned_verbatim():
    # Empty is a literal too — caller decides what to do with empty.
    assert resolve_secret("") == ""


def test_op_reference_calls_op_read():
    fake = subprocess.CompletedProcess(
        args=["op", "read", "op://Personal/Schwab/password"],
        returncode=0,
        stdout="my_secret_password\n",
        stderr="",
    )
    with patch("schwab_cli.secrets.subprocess.run", return_value=fake) as run:
        result = resolve_secret("op://Personal/Schwab/password")
    assert result == "my_secret_password"
    args, kwargs = run.call_args
    assert args[0] == ["op", "read", "op://Personal/Schwab/password"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is True


def test_op_missing_raises_secret_error():
    with patch("schwab_cli.secrets.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(SecretError, match="not found on PATH"):
            resolve_secret("op://X/Y/Z")


def test_op_failure_surfaces_stderr_in_secret_error():
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["op", "read", "op://X/Y/Z"],
        output="",
        stderr="[ERROR] item X not found\n",
    )
    with patch("schwab_cli.secrets.subprocess.run", side_effect=err):
        with pytest.raises(SecretError, match="item X not found"):
            resolve_secret("op://X/Y/Z")


def test_op_failure_with_no_stderr_uses_generic_message():
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["op", "read", "op://X/Y/Z"], output="", stderr=""
    )
    with patch("schwab_cli.secrets.subprocess.run", side_effect=err):
        with pytest.raises(SecretError, match="unknown error"):
            resolve_secret("op://X/Y/Z")
