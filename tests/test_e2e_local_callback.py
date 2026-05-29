"""End-to-end smoke test for the local callback flow.

Exercises the real wiring: a TLS ``CallbackServer`` (leaf minted by the
Phase 0 ``cert/generate`` code) → a browser GET over real HTTPS →
``get_auth_response`` capture → (stubbed) token exchange → saved session.

Hermetic: isolated ``SCHWAB_CLI_CONFIG_DIR``, no keychain (cert paths seam is
monkeypatched), no real Schwab (respx-stubbed token endpoint), no real browser
(``webbrowser.open`` no-op). The OAuth ``state`` is pinned via a monkeypatch so
the test can send a matching callback.
"""
from __future__ import annotations

import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import respx

from schwab_cli import auth_flows
from schwab_cli.cert.generate import (
    cert_to_pem,
    generate_ca,
    generate_leaf,
    key_to_pem,
)
from schwab_cli.config import Config
from schwab_cli.oauth import TOKEN_URL

_STATE = "E2ESTATE"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_leaf(tmp_path: Path) -> tuple[str, str]:
    ca = generate_ca()
    leaf = generate_leaf(ca)
    cert = tmp_path / "leaf.pem"
    key = tmp_path / "leaf-key.pem"
    cert.write_bytes(cert_to_pem(leaf.cert))
    key.write_bytes(key_to_pem(leaf.key))
    return str(cert), str(key)


def _cfg(port: int) -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri=f"https://127.0.0.1:{port}/schwab/callback",
        auth_flow="local_server",
    )


def _pin_seams(monkeypatch, certfile: str, keyfile: str) -> None:
    monkeypatch.setattr(
        auth_flows, "_resolve_cert_paths", lambda uri: (certfile, keyfile)
    )
    monkeypatch.setattr(
        auth_flows.secrets, "token_urlsafe", lambda n=32: _STATE
    )
    monkeypatch.setattr(auth_flows.webbrowser, "open", lambda url: True)


def _callback_url(port: int) -> str:
    return (
        f"https://127.0.0.1:{port}/schwab/callback"
        f"?code=E2ECODE&state={_STATE}"
    )


def test_e2e_capture_over_real_tls(tmp_path, monkeypatch):
    """get_auth_response binds a real TLS server and captures the redirect."""
    port = _free_port()
    certfile, keyfile = _write_leaf(tmp_path)
    _pin_seams(monkeypatch, certfile, keyfile)

    out: dict = {}

    def _run():
        try:
            out["result"] = auth_flows.get_auth_response(_cfg(port), manual=True)
        except Exception as e:  # noqa: BLE001
            out["exc"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Drive the browser GET over real HTTPS (trust the tmp leaf via verify=False).
    deadline = time.time() + 5
    resp = None
    while time.time() < deadline:
        try:
            resp = httpx.get(_callback_url(port), verify=False, timeout=2)
            break
        except httpx.ConnectError:
            time.sleep(0.05)

    t.join(timeout=5)
    assert resp is not None and resp.status_code == 200
    assert out.get("result") == {
        "kind": "code",
        "code": "E2ECODE",
        "state": _STATE,
    }, out


@respx.mock
def test_e2e_full_auth_to_session(tmp_path, monkeypatch):
    """perform_full_auth: capture → token exchange (stubbed) → saved session."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    port = _free_port()
    certfile, keyfile = _write_leaf(tmp_path)
    _pin_seams(monkeypatch, certfile, keyfile)

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 1800,
            },
        )
    )

    def _send():
        # Use stdlib urllib (not httpx) so respx does not intercept the
        # loopback callback request.
        ctx = ssl._create_unverified_context()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                urllib.request.urlopen(  # noqa: S310 — loopback test URL
                    _callback_url(port), context=ctx, timeout=2
                )
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.05)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    session = auth_flows.perform_full_auth(_cfg(port), manual=True)
    t.join(timeout=5)

    assert session.access_token == "AT"
    assert session.refresh_token == "RT"

    # The session was persisted to the isolated config dir.
    from schwab_cli.session import load as load_session

    saved = load_session()
    assert saved is not None
    assert saved.access_token == "AT"
