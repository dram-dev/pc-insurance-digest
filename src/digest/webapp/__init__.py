"""Local web observatory for the PC Digest warehouse.

A read-only, zero-extra-dependency web app (stdlib HTTP server + vendored D3)
that visualizes the digest's SQLite warehouse: news timing, signal scores,
prices vs. filings, loss triangles, and pipeline operations.

CLI: ``uv run digest web`` → http://127.0.0.1:8787
"""
