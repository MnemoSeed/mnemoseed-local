"""MCP gateway (A3 T3): a minimal, zero-new-dependency MCP stdio server.

``mcp_gateway.server`` speaks newline-delimited JSON-RPC 2.0 (MCP
``2024-11-05`` shape) over stdin/stdout and proxies the three daemon tools —
``recall`` / ``remember`` / ``dream_once`` — to the daemon REST with the
``mcp`` audit actor (design/01 §4.5, ingestion channel ③: the backup seat;
host hooks remain the capture mainline).
"""
