"""``schwab mcp --stdio`` — a full MCP server over stdio.

This is a **sibling** of the daemon's HTTP MCP server, not a proxy to it: it
serves the SAME tool implementations (``SchwabMcpServer``) directly over the
shared service layer.

- REST tools call Schwab through a client whose 401 path delegates token
  refresh to the daemon (``auth_delegate``) — it is never a second token
  owner.
- Local-DB tools (``dataset_*`` / ``screener_*``) read SQLite and need
  neither the daemon nor the network.
- ``stream_quote`` rides a :class:`RemoteStreamerBridge` that forwards to the
  daemon's single shared Schwab stream (Schwab allows one streamer/account).

Consequence: a daemon restart no longer breaks read tools. The stdio server
keeps serving on the session in ``session.json``; only genuine token refresh
(on expiry) or live streaming need the daemon to be up.
"""
from __future__ import annotations

import sys

from schwab_cli.auth_delegate import daemon_url


def _mcp_endpoint() -> str:
    """Daemon Streamable-HTTP endpoint (used only for stream_quote forwarding)."""
    return daemon_url() + "/mcp"


def run_stdio_server() -> None:
    """Entry point for ``schwab mcp --stdio``."""
    import anyio

    from schwab_cli import config as config_module
    from schwab_cli import session as session_module
    from schwab_cli.api.client import SchwabClient
    from schwab_cli.mcp_server.app import SchwabMcpServer
    from schwab_cli.mcp_server.logbook import LogBook
    from schwab_cli.mcp_server.remote_bridge import RemoteStreamerBridge
    from schwab_cli.notify import Notifier
    from schwab_cli.notify import config as notify_config

    cfg = config_module.load()
    if cfg is None:
        print(
            "schwab mcp --stdio: not configured. Run `schwab setup` first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Load the session as-is (don't gate on freshness / the daemon): the
    # client's 401 path delegates refresh to the daemon lazily, so the server
    # starts even if the daemon is momentarily down and local-DB tools keep
    # working regardless.
    session = session_module.load()
    if session is None:
        print(
            "schwab mcp --stdio: no session. Run `schwab auth` first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client = SchwabClient(cfg, session)  # refresh_hook=None → delegate on 401
    logbook = LogBook()  # → stderr; stdout is the MCP protocol channel
    notifier = Notifier(notify_config.NotificationConfig())  # inert; never notifies
    bridge = RemoteStreamerBridge(_mcp_endpoint())
    server = SchwabMcpServer(client, logbook, notifier=notifier, bridge=bridge)

    try:
        anyio.run(server.run_stdio)
    except KeyboardInterrupt:
        pass
