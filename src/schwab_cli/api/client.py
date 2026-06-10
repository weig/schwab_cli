from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import httpx

from schwab_cli import auth_delegate
from schwab_cli.config import Config
from schwab_cli.session import Session


class ApiError(Exception):
    """Raised on any Schwab API failure that isn't an auth-refresh case."""


class SessionExpired(ApiError):
    """Raised when the refresh token is rejected or the API still 401s after refresh.

    User must re-auth interactively (`schwab_cli auth --force`).
    """


@dataclass(frozen=True)
class AccountIds:
    """User-facing account_number <-> Schwab hashValue."""

    account_number: str
    hash_value: str


class SchwabClient:
    """Minimal auth-aware HTTP client for Schwab REST APIs.

    Handles Bearer-token injection, a single automatic refresh on 401, and
    mapping of HTTP / network errors to `ApiError` / `SessionExpired`.
    """

    TRADER_BASE = "https://api.schwabapi.com/trader/v1"
    MARKET_BASE = "https://api.schwabapi.com/marketdata/v1"

    def __init__(
        self,
        cfg: Config,
        session: Session,
        *,
        refresh_hook: Callable[[], Session | None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._session = session
        # 401 recovery path. ``None`` (the default) delegates to the
        # daemon via auth_delegate (in-process TokenManager inside the
        # daemon, HTTP elsewhere). The daemon's persistent client wires
        # its TokenManager's force_exchange here explicitly.
        self.refresh_hook = refresh_hook
        self._account_ids_cache: list[AccountIds] | None = None
        # Persistent HTTP/2 client. ``httpx.request`` (the module-level
        # helper) opens a fresh TCP+TLS connection per call, so each
        # Schwab call paid ~200–300 ms in handshake. A long-lived
        # client reuses the underlying connection for sequential
        # calls; with HTTP/2 it also multiplexes the parallel
        # ``order place`` fan-out (account/quote/chain) on a single
        # stream, dropping the per-arm handshake cost.
        #
        # ``http2`` requires the ``h2`` extra. We assume it's
        # installed (declared in ``pyproject.toml``) — the constructor
        # raises if it's not, which is loud-enough feedback to fix.
        self._http: httpx.Client | None = None

    @property
    def session(self) -> Session:
        return self._session

    def get(self, url: str, *, params: dict | None = None) -> dict | list:
        """Authed GET. Returns parsed JSON body. Raises ApiError/SessionExpired."""
        resp = self._authed_request("GET", url, params=params)
        return resp.json()

    def post(
        self, url: str, *, json: dict | None = None, params: dict | None = None,
    ) -> httpx.Response:
        """Authed POST. Returns the raw :class:`httpx.Response` so the caller
        can read headers (e.g. the ``Location`` header on a 201 from
        ``placeOrder`` / ``replaceOrder``) and decide whether to parse
        the body. Raises ApiError/SessionExpired on auth/network/HTTP
        failures.
        """
        return self._authed_request("POST", url, json=json, params=params)

    def put(
        self, url: str, *, json: dict | None = None, params: dict | None = None,
    ) -> httpx.Response:
        """Authed PUT. Returns the raw :class:`httpx.Response` so the
        caller can read headers — Schwab's ``replaceOrder`` returns 201
        with the new order id in the ``Location`` header, like
        ``placeOrder``.
        """
        return self._authed_request("PUT", url, json=json, params=params)

    def delete(self, url: str, *, params: dict | None = None) -> httpx.Response:
        """Authed DELETE. Returns raw :class:`httpx.Response` (Schwab's
        ``cancelOrder`` returns 200/204 with no body)."""
        return self._authed_request("DELETE", url, params=params)

    def _authed_request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        """Issue ``method`` against ``url`` with the bearer token; on 401,
        refresh once and retry. Returns the response only if it's < 400;
        otherwise raises :class:`ApiError` (or :class:`SessionExpired`
        for unrecoverable auth failures).
        """
        try:
            resp = self._request(method, url, params=params, json=json)
        except httpx.RequestError as e:
            raise ApiError(f"network: {type(e).__name__}") from e

        if resp.status_code == 401:
            self._refresh_or_expire()
            try:
                resp = self._request(method, url, params=params, json=json)
            except httpx.RequestError as e:
                raise ApiError(f"network: {type(e).__name__}") from e
            if resp.status_code == 401:
                raise SessionExpired(
                    "Session expired. Run `schwab_cli auth --force`."
                )

        if resp.status_code >= 400:
            body = (resp.text or "").splitlines()[0] if resp.text else ""
            raise ApiError(f"{resp.status_code} {body}".strip())

        return resp

    def _http_client(self) -> httpx.Client:
        """Return the shared HTTP/2 client, creating it on first use.

        Lazy so the client cost (TLS context, h2 import) doesn't hit
        unrelated commands that don't talk to Schwab. Connection
        pooling means subsequent calls reuse the open TCP+TLS;
        HTTP/2 means concurrent calls multiplex on one stream rather
        than each opening their own.
        """
        if self._http is None:
            self._http = httpx.Client(
                http2=True,
                timeout=30.0,
                limits=httpx.Limits(
                    max_keepalive_connections=4,
                    max_connections=8,
                    keepalive_expiry=60.0,
                ),
            )
        return self._http

    def close(self) -> None:
        """Close the underlying HTTP/2 client. Safe to call multiple
        times. Tests use this to release the connection pool between
        cases without leaking warnings."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "SchwabClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        # Release the HTTP/2 connection pool when used as a context
        # manager. Service-layer callers use `with SchwabClient(...)`
        # so a short-lived call never leaks the pool.
        self.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        return self._http_client().request(
            method,
            url,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {self._session.access_token}"},
            timeout=30.0,
        )

    def _refresh_or_expire(self) -> None:
        """Recover from a 401 by asking the token owner for a fresh session.

        Never exchanges tokens or writes session.json itself — the
        daemon's TokenManager is the single writer. ``refresh_hook``
        (when wired) is the in-process path; the default delegates via
        :mod:`schwab_cli.auth_delegate`.
        """
        if self.refresh_hook is not None:
            try:
                fresh = self.refresh_hook()
            except Exception:  # noqa: BLE001 — any hook failure ≡ no session
                fresh = None
        else:
            fresh = auth_delegate.request_refresh(
                on_unreachable=auth_delegate.automated_unreachable_notifier(),
            )
        if fresh is None:
            raise SessionExpired(
                "Session expired. Run `schwab_cli auth --force`."
            )
        self._session = fresh

    def _load_account_ids(self) -> list[AccountIds]:
        if self._account_ids_cache is None:
            raw = self.get(f"{self.TRADER_BASE}/accounts/accountNumbers")
            self._account_ids_cache = [
                AccountIds(account_number=item["accountNumber"], hash_value=item["hashValue"])
                for item in raw
            ]
        return self._account_ids_cache

    def account_ids(self) -> list[AccountIds]:
        """Return all (accountNumber, hashValue) pairs for this session. Cached."""
        return self._load_account_ids()

    def resolve_account(self, user_input: str) -> AccountIds:
        """Match user input against account_number (exact or suffix).

        Raises ApiError on 0 matches ("not found") or 2+ matches ("Multiple accounts match").
        """
        ids = self._load_account_ids()
        matches = [i for i in ids if i.account_number == user_input or i.account_number.endswith(user_input)]
        if not matches:
            available = ", ".join(f"...{i.account_number[-4:]}" for i in ids)
            raise ApiError(
                f"Account {user_input!r} not found. Available: {available}."
            )
        if len(matches) > 1:
            listing = ", ".join(m.account_number for m in matches)
            raise ApiError(
                f"Multiple accounts match {user_input!r}: {listing}. Specify more digits."
            )
        return matches[0]
