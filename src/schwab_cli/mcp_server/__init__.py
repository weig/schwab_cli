"""Schwab MCP server package.

Exposes the streaming WebSocket + REST tools to MCP clients over
SSE (primary) or stdio. Runs as a long-lived daemon for SSE mode;
see ``commands/mcp.py`` for the CLI entry point.
"""
