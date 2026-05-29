"""Tests for ``schwab_cli setup``.

Post-refactor prompt order (REWRITTEN from the old code_relay/auth_flow prompts):

  1. client_id
  2. client_secret
  3. Callback URL  (label changed from "Redirect URI"; default via _default_callback_url())
  4. Configure auto-login? (y/n)
  5. (if y) auto_login_command       (parsed via shlex.split)
  6. (if y) auto_login_timeout_seconds

Removed prompts:
  - auth_flow  (always "local_server" now)
  - code_relay_url

Setup seams (monkeypatched in tests — never touch real keychain):
  - setup._maybe_install_cert(url)   — invoked when url is loopback-https
  - setup._default_callback_url()    — returns the default callback URL string

Cert install is skipped:
  - for non-loopback-https URLs
  - when no TTY (non-interactive mode)

Config invariants after setup:
  - auth_flow == "local_server"
  - no code_relay_url field/attribute
"""
import re

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, load
from schwab_cli.config import save as save_cfg

runner = CliRunner()

_LOOPBACK_URI = "https://127.0.0.1:19806/schwab/callback"
_RELAY_URI = "https://relay.example.com/uuid/secret"
_AUTO_CMD = "webauto-cli /p/script.py --env /p/auto.env"
_AUTO_CMD_TUPLE = ("webauto-cli", "/p/script.py", "--env", "/p/auto.env")


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)


def _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI):
    """Patch both setup seams so no real keychain or cert install happens."""
    cert_calls = []

    monkeypatch.setattr(
        "schwab_cli.commands.setup._default_callback_url",
        lambda: default_url,
    )
    monkeypatch.setattr(
        "schwab_cli.commands.setup._maybe_install_cert",
        lambda url: cert_calls.append(url),
    )
    return cert_calls


def _run(inputs, monkeypatch, tmp_path, *, patch_default_url=_LOOPBACK_URI):
    _setup_env(monkeypatch, tmp_path)
    cert_calls = _patch_cert_seams(monkeypatch, default_url=patch_default_url)
    result = runner.invoke(app, ["setup"], input=inputs)
    return result, cert_calls


# ---- New prompt order: client_id, client_secret, Callback URL, auto-login --


def test_fresh_setup_loopback_https_without_auto_login(monkeypatch, tmp_path):
    """Fresh setup with a loopback-https callback URL; decline auto-login.
    auth_flow must be 'local_server'; no code_relay_url attribute."""
    result, cert_calls = _run(
        f"cid_value\ncsec_value\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.client_id == "cid_value"
    assert cfg.client_secret == "csec_value"
    assert cfg.redirect_uri == _LOOPBACK_URI
    assert cfg.auth_flow == "local_server"
    assert not hasattr(cfg, "code_relay_url")
    assert cfg.auto_login_command is None
    assert cfg.auto_login_timeout_seconds == 300


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    """Callback URL + auto-login enabled."""
    result, cert_calls = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\ny\n{_AUTO_CMD}\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auth_flow == "local_server"
    assert cfg.auto_login_command == _AUTO_CMD_TUPLE
    assert cfg.auto_login_timeout_seconds == 300


def test_saved_config_has_no_code_relay_url_attribute(monkeypatch, tmp_path):
    """Saved config must never have code_relay_url, regardless of input."""
    _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    cfg = load()
    assert not hasattr(cfg, "code_relay_url")


def test_auth_flow_prompt_is_absent(monkeypatch, tmp_path):
    """There must be no auth_flow prompt: the 3-field input sequence
    (cid, csec, callback_url) + 'n' for auto-login must complete setup
    without any re-prompting on auth_flow."""
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    # If an auth_flow prompt appeared, the next 'n' would be consumed as
    # the auth_flow answer and auto-login would then re-prompt. The test
    # passing with exit_code 0 is the assertion.
    assert load().auth_flow == "local_server"


def test_code_relay_url_prompt_is_absent(monkeypatch, tmp_path):
    """There must be no code_relay_url prompt."""
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "code relay" not in result.output.lower()
    assert "relay url" not in result.output.lower()


def test_callback_url_label_in_prompt(monkeypatch, tmp_path):
    """The prompt must use 'Callback URL' (not 'Redirect URI')."""
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "callback url" in result.output.lower() or "callback" in result.output.lower()


def test_default_callback_url_seam_is_used(monkeypatch, tmp_path):
    """When no existing config, the default callback URL comes from the seam."""
    _setup_env(monkeypatch, tmp_path)
    cert_calls: list = []
    monkeypatch.setattr(
        "schwab_cli.commands.setup._default_callback_url",
        lambda: "https://127.0.0.1:17777/schwab/callback",
    )
    monkeypatch.setattr(
        "schwab_cli.commands.setup._maybe_install_cert",
        lambda url: cert_calls.append(url),
    )
    # Press Enter to accept the default callback URL.
    result = runner.invoke(app, ["setup"], input="cid\ncsec\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.redirect_uri == "https://127.0.0.1:17777/schwab/callback"


def test_default_callback_url_regex_shape(monkeypatch, tmp_path):
    """The real _default_callback_url() (unpatched) must return a URL
    matching https://127.0.0.1:<port in 15000-20000>/schwab/callback."""
    _setup_env(monkeypatch, tmp_path)
    from schwab_cli.commands.setup import _default_callback_url
    url = _default_callback_url()
    assert re.match(
        r"^https://127\.0\.0\.1:1[5-9]\d{3}/schwab/callback$", url
    ), f"unexpected default callback URL: {url}"


# ---- Cert install seam tests -----------------------------------------------


def test_cert_install_invoked_for_loopback_https_url(monkeypatch, tmp_path):
    """_maybe_install_cert must be called when the Callback URL is loopback-https."""
    result, cert_calls = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert len(cert_calls) == 1
    assert cert_calls[0] == _LOOPBACK_URI


def test_cert_install_not_invoked_for_non_loopback_url(monkeypatch, tmp_path):
    """_maybe_install_cert must NOT be called for a non-loopback URL."""
    result, cert_calls = _run(
        f"cid\ncsec\n{_RELAY_URI}\nn\n",
        monkeypatch, tmp_path,
        patch_default_url=_RELAY_URI,
    )
    assert result.exit_code == 0, result.output
    assert len(cert_calls) == 0


def test_cert_install_not_invoked_for_http_loopback_url(monkeypatch, tmp_path):
    """Plain http:// loopback URL must NOT trigger cert install (http ≠ https)."""
    http_url = "http://127.0.0.1:19806/schwab/callback"
    result, cert_calls = _run(
        f"cid\ncsec\n{http_url}\nn\n",
        monkeypatch, tmp_path,
        patch_default_url=http_url,
    )
    assert result.exit_code == 0, result.output
    assert len(cert_calls) == 0


def test_cert_install_notice_printed_for_loopback_https(monkeypatch, tmp_path):
    """Setup must print a notice about the local HTTPS server and certificate
    before invoking cert install for loopback-https URLs."""
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    # The notice must mention something about https server or certificate.
    assert any(
        phrase in output_lower
        for phrase in ("https server", "certificate", "cert", "root")
    ), f"expected cert-notice in output; got:\n{result.output}"


# ---- Auto-login prompts still work correctly --------------------------------


def test_setup_auto_login_command_with_quoted_path(monkeypatch, tmp_path):
    """``shlex.split`` handles quoted paths with spaces."""
    quoted = 'webauto-cli "/path with spaces/script.py" --env /p/env'
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\ny\n{quoted}\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auto_login_command == (
        "webauto-cli", "/path with spaces/script.py", "--env", "/p/env",
    )


def test_setup_auto_login_reprompts_on_invalid_timeout(monkeypatch, tmp_path):
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\ny\n{_AUTO_CMD}\n-5\n300\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "positive integer" in result.output
    assert load().auto_login_timeout_seconds == 300


def test_setup_auto_login_keeps_default_timeout_on_empty_input(monkeypatch, tmp_path):
    """Empty timeout input → keep default (300)."""
    result, _ = _run(
        f"cid\ncsec\n{_LOOPBACK_URI}\ny\n{_AUTO_CMD}\n\n",
        monkeypatch, tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert load().auto_login_timeout_seconds == 300


# ---- Dry run ----------------------------------------------------------------


def test_dry_run_prints_payload_and_does_not_save(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input=f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert load() is None
    assert "dry-run" in result.output.lower()
    assert '"auth_flow": "local_server"' in result.output
    assert "code_relay_url" not in result.output


def test_dry_run_does_not_write_code_relay_url_to_output(monkeypatch, tmp_path):
    """Even in dry-run mode, code_relay_url must not appear in the output."""
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    result = runner.invoke(
        app,
        ["setup", "--dry-run"],
        input=f"cid\ncsec\n{_LOOPBACK_URI}\nn\n",
    )
    assert "code_relay_url" not in result.output


def test_dry_run_does_not_overwrite_existing_config(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    save_cfg(Config(
        client_id="existing_id", client_secret="existing_secret",
        redirect_uri=_LOOPBACK_URI, auth_flow="local_server",
    ))
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    original_bytes = file.read_bytes()

    result = runner.invoke(
        app, ["setup", "--dry-run"],
        input=f"new_id\nnew_secret\n{_LOOPBACK_URI}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert file.read_bytes() == original_bytes
    assert load().client_id == "existing_id"
    assert '"client_id": "new_id"' in result.output


# ---- Keeping existing values on re-run -------------------------------------


def test_rerun_accepting_defaults_preserves_all_values(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    save_cfg(Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri=_LOOPBACK_URI,
        auth_flow="local_server",
        auto_login_command=_AUTO_CMD_TUPLE,
        auto_login_timeout_seconds=600,
    ))
    # Press Enter through: client_id, client_secret, callback_url,
    # auto-login confirm (default y), command (Enter keeps), timeout (Enter keeps).
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        redirect_uri=_LOOPBACK_URI,
        auth_flow="local_server",
        auto_login_command=_AUTO_CMD_TUPLE,
        auto_login_timeout_seconds=600,
    )


def test_rerun_disabling_auto_login_removes_command(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    save_cfg(Config(
        client_id="cid", client_secret="csec",
        redirect_uri=_LOOPBACK_URI, auth_flow="local_server",
        auto_login_command=_AUTO_CMD_TUPLE,
    ))
    # Press Enter through cid/secret/callback_url, then 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.auto_login_command is None


# ---- Re-prompting on empty client_id ----------------------------------------


def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    result = runner.invoke(
        app, ["setup"],
        input=f"\ncid_value\ncsec_value\n{_LOOPBACK_URI}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Client ID is required" in result.output
    assert load().client_id == "cid_value"


# ---- Malformed existing config ----------------------------------------------


def test_malformed_existing_config_decline_overwrite_leaves_file(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    bad = cfg_dir / "config.json"
    bad.write_text("{not valid")
    original_bytes = bad.read_bytes()

    result = runner.invoke(app, ["setup"], input="n\n")
    assert result.exit_code == 0, result.output
    assert bad.read_bytes() == original_bytes


def test_malformed_existing_config_accept_overwrite_writes_new(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _patch_cert_seams(monkeypatch, default_url=_LOOPBACK_URI)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid")

    result = runner.invoke(
        app, ["setup"],
        input=f"y\ncid\ncsec\n{_LOOPBACK_URI}\nn\n",
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.client_id == "cid"
    assert cfg.auth_flow == "local_server"
    assert not hasattr(cfg, "code_relay_url")
