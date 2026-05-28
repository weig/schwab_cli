"""Schwab MCP server package.

Exposes the streaming WebSocket + REST tools to MCP clients over
Streamable HTTP (single ``/mcp`` endpoint). Runs as a long-lived
daemon; see ``commands/mcp.py`` for the CLI entry point.
"""
