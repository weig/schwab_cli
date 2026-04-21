from __future__ import annotations

import time

import httpx

from schwab_cli import oauth
from schwab_cli.config import Config
from schwab_cli.session import Session, save as save_session


class ApiError(Exception):
    """Raised on any Schwab API failure that isn't an auth-refresh case."""


class SessionExpired(ApiError):
    """Raised when the refresh token is rejected or the API still 401s after refresh.

    User must re-auth interactively (`schwab_cli auth --force`).
    """


class SchwabClient:
    """Minimal auth-aware HTTP client for Schwab REST APIs.

    Handles Bearer-token injection, a single automatic refresh on 401, and
    mapping of HTTP / network errors to `ApiError` / `SessionExpired`.
    """

    def __init__(self, cfg: Config, session: Session) -> None:
        self._cfg = cfg
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get(self, url: str, *, params: dict | None = None) -> dict | list:
        """Authed GET. Returns parsed JSON body. Raises ApiError/SessionExpired."""
        try:
            resp = self._request("GET", url, params=params)
        except httpx.RequestError as e:
            raise ApiError(f"network: {type(e).__name__}") from e

        if resp.status_code == 401:
            self._refresh_or_expire()
            try:
                resp = self._request("GET", url, params=params)
            except httpx.RequestError as e:
                raise ApiError(f"network: {type(e).__name__}") from e
            if resp.status_code == 401:
                raise SessionExpired(
                    "Session expired. Run `schwab_cli auth --force`."
                )

        if resp.status_code >= 400:
            body = (resp.text or "").splitlines()[0] if resp.text else ""
            raise ApiError(f"{resp.status_code} {body}".strip())

        return resp.json()

    def _request(self, method: str, url: str, *, params: dict | None = None) -> httpx.Response:
        return httpx.request(
            method,
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._session.access_token}"},
            timeout=30.0,
        )

    def _refresh_or_expire(self) -> None:
        try:
            tr = oauth.refresh(self._cfg, self._session.refresh_token)
        except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
            raise SessionExpired(
                "Session expired. Run `schwab_cli auth --force`."
            ) from e
        self._session = Session.from_token_response(tr, now=int(time.time()))
        save_session(self._session)
