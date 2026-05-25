"""Domain-agnostic DB connection + write helpers.

Owns: `get_conn`, `init_db_with_migrations`, `utcnow_iso`, `upsert_items`,
`log_run`, `item_stats`, `recent_items`, `recent_kept_titles`.

Domain `db.py` modules import these and layer their own migrations on top.
The `init_db_with_migrations` helper applies BASE_SCHEMA, then runs the
caller-supplied migrations list, swallowing "duplicate column" errors so
the apply stays idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from digest_core.db.schema import BASE_SCHEMA
from digest_core.types import IngestedItem


def init_db_with_migrations(
    db_path: Path,
    migrations: Iterable[str] = (),
) -> None:
    """Create the DB + apply BASE_SCHEMA + run caller migrations idempotently."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(BASE_SCHEMA)
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context manager for a DB connection with row factory + WAL set."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_items(conn: sqlite3.Connection, items: Iterable[IngestedItem]) -> int:
    """Insert new items via INSERT OR IGNORE. Returns count of new rows.

    Takes a connection so callers can compose with other writes inside the
    same `with get_conn() as conn:` block (matters when a domain wants to
    sink to bronze after the SQLite write commits — see DatabricksSink).
    """
    sql = """
        INSERT OR IGNORE INTO items
            (source, source_id, url, title, author, content, published_at, metadata_json)
        VALUES
            (:source, :source_id, :url, :title, :author, :content, :published_at, :metadata_json)
    """
    inserted = 0
    for item in items:
        d = asdict(item)
        d["metadata_json"] = json.dumps(d.pop("metadata", {}) or {})
        if isinstance(d.get("published_at"), datetime):
            d["published_at"] = d["published_at"].isoformat()
        cur = conn.execute(sql, d)
        if cur.rowcount:
            inserted += 1
    return inserted


def log_run(
    conn: sqlite3.Connection,
    run_type: str,
    source: str,
    items_fetched: int,
    items_new: int,
    duration_ms: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append a row to run_log."""
    conn.execute(
        """
        INSERT INTO run_log
            (run_type, source, items_fetched, items_new, duration_ms, status, error)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_type, source, items_fetched, items_new, duration_ms, status, error),
    )


def item_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Item counts grouped by source."""
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY n DESC"
    ).fetchall()
    return {row["source"]: row["n"] for row in rows}


def recent_items(
    conn: sqlite3.Connection,
    source: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Most recently ingested items, optionally filtered by source."""
    sql = "SELECT id, source, title, url, published_at, ingested_at FROM items"
    params: tuple = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY ingested_at DESC LIMIT ?"
    params = (*params, limit)
    return conn.execute(sql, params).fetchall()


def recent_kept_titles(conn: sqlite3.Connection, hours: int = 24) -> list[str]:
    """Titles of kept items in the last N hours — fuel for near-dupe detection."""
    rows = conn.execute(
        """
        SELECT title FROM items
        WHERE triage_decision = 'keep'
          AND triaged_at >= datetime('now', ?)
        """,
        (f"-{hours} hours",),
    ).fetchall()
    return [r["title"] for r in rows if r["title"]]
