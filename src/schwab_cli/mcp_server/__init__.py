"""Schwab MCP server package.

Exposes the streaming WebSocket + REST tools to MCP clients over
Streamable HTTP (primary, single ``/mcp`` endpoint) or stdio. Runs as
a long-lived daemon for HTTP mode; see ``commands/mcp.py`` for the
CLI entry point.
"""
