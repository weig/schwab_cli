"""Layer-2 service base — shared auth boilerplate + injectable output sink.

Factors out the ``cfg = config.load(); if None: raise NotConfigured;
session = service.auth.get_session(cfg); with SchwabClient(cfg, session)
as client: ...`` boilerplate that every service function used to repeat
(PR #52 review #10), and provides an injectable output sink so progress /
notice text can go to stderr from the CLI but to a no-op (or logger) from
MCP / REST (review #11).

Subclasses call ``with self._authed_client() as client:`` instead of the
inline config / get_session / SchwabClient dance, and emit user-facing
progress / notices via ``self._out.progress(...)`` / ``self._out.info(...)``.
"""

from __future__ import annotations

import contextlib
from typing import Iterator, Protocol

from schwab_cli import config as config_module
from schwab_cli.api.client import SchwabClient
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured


class OutputSink(Protocol):
    """Sink for user-facing service output.

    ``info`` is for one-off notices (e.g. the backfill summary line);
    ``progress`` is for per-step progress lines streamed during a long
    operation. The CLI wires these to ``typer.secho``; MCP / REST use the
    :class:`NullSink` so nothing pollutes their stdout payloads.
    """

    def info(self, message: str) -> None: ...      # notices (e.g. backfill summary)

    def progress(self, message: str) -> None: ...   # per-step progress lines


class NullSink:
    """Default sink — swallows output (MCP / REST use this)."""

    def info(self, message: str) -> None:
        ...

    def progress(self, message: str) -> None:
        ...


class BaseService:
    """Common base for every Layer-2 service.

    Holds the injected :class:`OutputSink` (defaulting to :class:`NullSink`)
    and yields an authed :class:`SchwabClient` via :meth:`_authed_client`,
    so subclasses never repeat the config / session / client boilerplate.
    """

    def __init__(self, *, out: OutputSink | None = None) -> None:
        self._out: OutputSink = out or NullSink()

    @contextlib.contextmanager
    def _authed_client(self) -> Iterator[SchwabClient]:
        """Yield an authed :class:`SchwabClient`.

        Loads config (-> :class:`NotConfigured` if missing), obtains a
        usable session (minted / raised via :mod:`schwab_cli.service.auth`),
        and opens the client as a context manager so the connection pool is
        always closed — even when the body raises.

        Config / session are reached through the module attributes
        (``config_module.load`` / ``service_auth.get_session``) so the
        characterization tests' definition-site patches keep working.
        """
        cfg = config_module.load()
        if cfg is None:
            raise NotConfigured
        session = service_auth.get_session(cfg)
        with SchwabClient(cfg, session) as client:
            yield client
