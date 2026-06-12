"""Stdlib HTTP server for the web observatory.

Zero new dependencies: ``ThreadingHTTPServer`` + a small JSON router over
``webapp.api``. Every request opens its own read-only SQLite connection
(``mode=ro``) — the server physically cannot mutate the warehouse. Static
assets are served from the packaged ``static/`` directory with path-traversal
protection.

CLI entry: ``digest web`` (see cli.py).
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from digest.config import settings
from digest.webapp import api

STATIC_DIR = Path(__file__).parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _ro_conn(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"No digest DB at {db_path}. Run `uv run digest init-db` first."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _q_int(q: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(q[key][0])
    except (KeyError, ValueError, IndexError):
        return default


def _q_str(q: dict[str, list[str]], key: str, default: str = "") -> str:
    return q.get(key, [default])[0]


def _q_list(q: dict[str, list[str]], key: str) -> list[str] | None:
    raw = _q_str(q, key)
    return [t for t in raw.split(",") if t] or None


# Route table: name → (conn, parsed query params) → JSON-serializable payload.
ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, list[str]]], Any]] = {
    "meta": lambda c, q: api.meta(c),
    "timeline": lambda c, q: api.timeline(c, days=_q_int(q, "days", 90)),
    "events": lambda c, q: api.events(c, days=_q_int(q, "days", 90),
                                      limit=min(_q_int(q, "limit", 400), 2000)),
    "cadence": lambda c, q: api.cadence(c, days=_q_int(q, "days", 90)),
    "latency": lambda c, q: api.latency(c, days=_q_int(q, "days", 90)),
    "regimes": lambda c, q: api.regimes(c),
    "leaderboard": lambda c, q: api.leaderboard(c, days=_q_int(q, "days", 90),
                                                limit=min(_q_int(q, "limit", 50), 500)),
    "score-distribution": lambda c, q: api.score_distribution(c, days=_q_int(q, "days", 90)),
    "prices": lambda c, q: api.prices(c, days=_q_int(q, "days", 365),
                                      tickers=_q_list(q, "tickers")),
    "price-events": lambda c, q: api.price_events(c, days=_q_int(q, "days", 365)),
    "forecasts": lambda c, q: api.forecasts(c),
    "triangle-catalog": lambda c, q: api.triangle_catalog(c),
    "triangle": lambda c, q: api.triangle(c, insurer=_q_str(q, "insurer"),
                                          lob=_q_str(q, "lob"),
                                          metric=_q_str(q, "metric", "incurred")),
    "freq-sev": lambda c, q: api.freq_sev(c, insurer=_q_str(q, "insurer")),
    "freq-sev-insurers": lambda c, q: api.freq_sev_insurers(c),
    "reserving": lambda c, q: api.reserving(c),
    "severity": lambda c, q: api.severity(c),
    "ops-runs": lambda c, q: api.ops_runs(c, days=_q_int(q, "days", 30)),
    "ops-funnel": lambda c, q: api.ops_funnel(c, days=_q_int(q, "days", 30)),
    "ops-summarizer": lambda c, q: api.ops_summarizer(c, days=_q_int(q, "days", 30)),
    "outcomes": lambda c, q: api.outcomes(c),
}


class ObservatoryHandler(BaseHTTPRequestHandler):
    """Routes /api/<name> to the query layer; everything else is static."""

    server_version = "PCDigestObservatory/1.0"
    db_path: Path  # set by make_server on the handler class

    # ── plumbing ────────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        pass

    def _send(self, status: int, body: bytes, content_type: str,
              cache: str = "no-cache") -> None:
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        encoded = gzip.compress(body) if accepts_gzip and len(body) > 1024 else None
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoded is not None:
            self.send_header("Content-Encoding", "gzip")
            body = encoded
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # ── routing ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._handle_api(parsed.path[len("/api/"):], parse_qs(parsed.query))
            else:
                self._handle_static(parsed.path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # surface, never crash the thread
            try:
                self._send_json({"error": str(exc)}, status=500)
            except Exception:
                pass

    def _handle_api(self, name: str, query: dict[str, list[str]]) -> None:
        route = ROUTES.get(name)
        if route is None:
            self._send_json({"error": f"unknown endpoint: {name}"}, status=404)
            return
        conn = _ro_conn(self.db_path)
        try:
            self._send_json(route(conn, query))
        finally:
            conn.close()

    def _handle_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        # Resolve inside STATIC_DIR only — reject traversal.
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(STATIC_DIR.resolve()) or not candidate.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = _CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        cache = "max-age=86400" if candidate.parts[-2] == "vendor" else "no-cache"
        self._send(200, candidate.read_bytes(), ctype, cache=cache)


def make_server(host: str = "127.0.0.1", port: int = 8787,
                db_path: Path | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (ObservatoryHandler,),
                   {"db_path": Path(db_path or settings.db_path)})
    return ThreadingHTTPServer((host, port), handler)


def serve(host: str = "127.0.0.1", port: int = 8787,
          db_path: Path | None = None, open_browser: bool = False) -> None:
    httpd = make_server(host, port, db_path)
    url = f"http://{host}:{port}"
    print(f"PC Digest Observatory → {url}  (read-only on {db_path or settings.db_path})")
    if open_browser:
        import webbrowser
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
