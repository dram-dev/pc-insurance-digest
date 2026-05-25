"""SQLite schema and helpers. Raw sqlite3 — no ORM, keeps things boring."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from digest.config import settings
from digest.sinks import sink

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT,
    title           TEXT NOT NULL,
    author          TEXT,
    content         TEXT,
    published_at    TEXT,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json   TEXT,
    topic           TEXT,
    summary         TEXT,
    why_it_matters  TEXT,
    confidence      TEXT,
    see_also        TEXT,
    triage_score    REAL,
    triage_decision TEXT,
    triaged_at      TEXT,
    summarized_at   TEXT,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_source        ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_published     ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_topic         ON items(topic);
CREATE INDEX IF NOT EXISTS idx_items_ingested      ON items(ingested_at);

CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    run_type        TEXT NOT NULL,
    source          TEXT NOT NULL,
    items_fetched   INTEGER,
    items_new       INTEGER,
    duration_ms     INTEGER,
    status          TEXT NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runlog_run_at ON run_log(run_at);

CREATE TABLE IF NOT EXISTS fred_baseline (
    series_id       TEXT PRIMARY KEY,
    mean_delta      REAL,
    stddev_delta    REAL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- For Phase 2 cost/usage tracking on the summarizer step.
CREATE TABLE IF NOT EXISTS summarizer_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    backend         TEXT NOT NULL,
    item_id         INTEGER NOT NULL,
    duration_ms     INTEGER,
    input_chars     INTEGER,
    output_chars    INTEGER,
    status          TEXT NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sumlog_run_at ON summarizer_log(run_at);
"""

# Phase 1 → Phase 2 migration. Idempotent.
MIGRATIONS = [
    "ALTER TABLE items ADD COLUMN confidence TEXT",
    "ALTER TABLE items ADD COLUMN see_also TEXT",
    "ALTER TABLE items ADD COLUMN triage_score REAL",
    "ALTER TABLE items ADD COLUMN triage_decision TEXT",
    "ALTER TABLE items ADD COLUMN triaged_at TEXT",
    "ALTER TABLE items ADD COLUMN summarized_at TEXT",
    "ALTER TABLE items ADD COLUMN obsidian_written_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_items_triage ON items(triage_decision)",
    "CREATE INDEX IF NOT EXISTS idx_items_obsidian ON items(obsidian_written_at)",
    # Phase 4: connection threads + weekly synthesis
    """CREATE TABLE IF NOT EXISTS daily_connections (
        date         TEXT PRIMARY KEY,
        threads_json TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Phase 5: macro regime classifier
    """CREATE TABLE IF NOT EXISTS macro_regime (
        week         TEXT PRIMARY KEY,
        regime       TEXT NOT NULL,
        signals_json TEXT NOT NULL,
        narrative    TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Idea 3: multi-persona ensemble scores
    "ALTER TABLE items ADD COLUMN ensemble_scores TEXT",
    "ALTER TABLE items ADD COLUMN ensemble_consensus REAL",
    "ALTER TABLE items ADD COLUMN ensemble_dispersion REAL",
    # Idea 1: TF-IDF narrative cluster label
    "ALTER TABLE items ADD COLUMN cluster_id TEXT",
    # Idea 2: signal outcome tracking
    """CREATE TABLE IF NOT EXISTS signal_outcomes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id      INTEGER NOT NULL REFERENCES items(id),
        checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
        horizon_days INTEGER NOT NULL DEFAULT 7,
        outcome      TEXT NOT NULL,
        original_z   REAL,
        followup_z   REAL,
        magnitude    REAL,
        UNIQUE(item_id, horizon_days)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_item ON signal_outcomes(item_id)",
    # Feature 1: financial sentiment
    "ALTER TABLE items ADD COLUMN sentiment_label TEXT",
    "ALTER TABLE items ADD COLUMN sentiment_score REAL",
    # Feature 3: entity extraction
    "ALTER TABLE items ADD COLUMN entities_json TEXT",
    # Feature 2: forward event calendar
    """CREATE TABLE IF NOT EXISTS upcoming_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type    TEXT NOT NULL,
        event_date    TEXT NOT NULL,
        title         TEXT NOT NULL,
        symbol        TEXT,
        metadata_json TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(event_type, event_date, title)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_date ON upcoming_events(event_date)",
    # Wave 2: PC two-axis regime detector (market_cycle × cat_load)
    """CREATE TABLE IF NOT EXISTS regime_signals (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        as_of             TEXT NOT NULL,
        market_cycle      TEXT NOT NULL,
        cat_load          TEXT NOT NULL,
        market_cycle_mult REAL NOT NULL,
        cat_load_mult     REAL NOT NULL,
        multiplier        REAL NOT NULL,
        evidence_json     TEXT,
        source            TEXT NOT NULL DEFAULT 'detector'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_regime_signals_as_of ON regime_signals(as_of DESC)",
    # Wave 2: per-item leaderboard score history
    """CREATE TABLE IF NOT EXISTS signal_scores (
        item_id          INTEGER NOT NULL,
        computed_at      TEXT NOT NULL DEFAULT (datetime('now')),
        score            REAL NOT NULL,
        source_mult      REAL,
        regime_mult      REAL,
        topic_relevance  REAL,
        recency          REAL,
        llm_judgment     REAL,
        topic_boost      REAL,
        burden_boost     REAL,
        PRIMARY KEY(item_id, computed_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_signal_scores_score ON signal_scores(score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signal_scores_computed ON signal_scores(computed_at DESC)",
    # Wave 2: materiality score from summarizer (feeds llm_judgment factor)
    "ALTER TABLE items ADD COLUMN materiality_score REAL",
    # Wave 2 lite: Regulatory Sonar — burden classification on regulatory_rate items
    "ALTER TABLE items ADD COLUMN burden_direction TEXT",   # increasing|neutral|decreasing|null
    "ALTER TABLE items ADD COLUMN burden_intensity TEXT",   # high|medium|low|null
    "CREATE INDEX IF NOT EXISTS idx_items_burden ON items(burden_intensity)",
    # Wave 2.x: insurer-priority + inflation-keyword boosts on signal_scores
    "ALTER TABLE signal_scores ADD COLUMN insurer_boost REAL DEFAULT 1.0",
    "ALTER TABLE signal_scores ADD COLUMN inflation_boost REAL DEFAULT 1.0",
    # Score Higher review (2026-05-24): regulatory/state-action keyword boost
    "ALTER TABLE signal_scores ADD COLUMN regulatory_boost REAL DEFAULT 1.0",
]


def init_db(db_path: Path | None = None) -> None:
    """Create DB file and schema if missing. Apply Phase-2 migrations idempotently."""
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Run ALTERs; ignore "duplicate column" errors so it stays idempotent
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager for a DB connection with row factory set."""
    path = db_path or settings.db_path
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_items(items: Iterable["IngestedItem"]) -> int:  # noqa: F821
    """Insert new items, ignore duplicates. Returns count of new rows."""
    items_list = list(items)
    if not items_list:
        return 0
    sql = """
        INSERT OR IGNORE INTO items
            (source, source_id, url, title, author, content, published_at, metadata_json)
        VALUES
            (:source, :source_id, :url, :title, :author, :content, :published_at, :metadata_json)
    """
    inserted = 0
    with get_conn() as conn:
        for item in items_list:
            d = asdict(item)
            d["metadata_json"] = json.dumps(d.pop("metadata", {}) or {})
            if isinstance(d.get("published_at"), datetime):
                d["published_at"] = d["published_at"].isoformat()
            cur = conn.execute(sql, d)
            if cur.rowcount:
                inserted += 1
    # Bronze sink: every ingested item, including soon-to-be-dropped ones.
    sink.write_ingested(items_list)
    return inserted


def log_run(
    run_type: str,
    source: str,
    items_fetched: int,
    items_new: int,
    duration_ms: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append a row to run_log."""
    sql = """
        INSERT INTO run_log
            (run_type, source, items_fetched, items_new, duration_ms, status, error)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
    """
    with get_conn() as conn:
        conn.execute(sql, (run_type, source, items_fetched, items_new, duration_ms, status, error))
    # Bronze telemetry — derive started_at from duration so the row is complete.
    ended = datetime.now(timezone.utc)
    started = ended - timedelta(milliseconds=duration_ms)
    sink.write_telemetry({
        "run_id":       f"{started.isoformat(timespec='seconds')}-{source}",
        "stage":        "ingest",
        "source":       source,
        "started_at":   started.isoformat(timespec="seconds"),
        "ended_at":     ended.isoformat(timespec="seconds"),
        "duration_ms":  duration_ms,
        "items_in":     items_fetched,
        "items_out":    items_new,
        "errors":       0 if status == "ok" else 1,
        "error_detail": error,
        "model_id":     None,
    })


def item_stats() -> dict[str, int]:
    """Return item counts grouped by source."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY n DESC"
        ).fetchall()
    return {row["source"]: row["n"] for row in rows}


def recent_items(source: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """Return most recently ingested items, optionally filtered by source."""
    sql = "SELECT id, source, title, url, published_at, ingested_at FROM items"
    params: tuple = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY ingested_at DESC LIMIT ?"
    params = (*params, limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Phase 2 helpers ────────────────────────────────────────────────────


def items_needing_triage(limit: int = 200) -> list[sqlite3.Row]:
    """Items ingested within the lookback window with no triage decision yet."""
    lookback = f"-{settings.triage_lookback_hours} hours"
    sql = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, metadata_json
        FROM items
        WHERE triage_decision IS NULL
          AND ingested_at >= datetime('now', ?)
        ORDER BY ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (lookback, limit)).fetchall()


def items_for_signals() -> list[sqlite3.Row]:
    """All summarized, kept items for signal scoring (no limit — scored in Python)."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at,
               topic, summary, why_it_matters, confidence, see_also,
               triage_score, materiality_score,
               burden_direction, burden_intensity, metadata_json,
               ensemble_consensus, ensemble_dispersion, cluster_id,
               sentiment_label, sentiment_score
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
        ORDER BY triage_score DESC, ingested_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def recent_kept_titles(hours: int = 24) -> list[str]:
    """Titles of kept items from the last N hours, for near-duplicate detection."""
    sql = """
        SELECT title FROM items
        WHERE triage_decision = 'keep'
          AND triaged_at >= datetime('now', ?)
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (f"-{hours} hours",)).fetchall()
    return [r["title"] for r in rows if r["title"]]


def items_needing_ensemble(limit: int = 200) -> list[sqlite3.Row]:
    """Kept + summarized items with no ensemble score yet."""
    sql = """
        SELECT id, source, title, topic, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND ensemble_consensus IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_ensemble(
    item_id: int, scores_json: str, consensus: float, dispersion: float
) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE items
               SET ensemble_scores = ?, ensemble_consensus = ?, ensemble_dispersion = ?
               WHERE id = ?""",
            (scores_json, consensus, dispersion, item_id),
        )


def items_for_clustering() -> list[sqlite3.Row]:
    """All kept+summarized items for TF-IDF clustering."""
    sql = """
        SELECT id, title, summary
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
        ORDER BY ingested_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def update_cluster_ids(id_to_label: dict[int, str]) -> None:
    if not id_to_label:
        return
    with get_conn() as conn:
        for item_id, label in id_to_label.items():
            conn.execute(
                "UPDATE items SET cluster_id = ? WHERE id = ?",
                (label, item_id),
            )


_VALID_OUTCOME_KEYS = frozenset({"series_id", "symbol", "contract"})


def items_for_outcome_check(horizon_days: int = 7, limit: int = 500) -> list[sqlite3.Row]:
    """FRED/CBOE/CFTC kept items old enough to check, with no outcome yet for this horizon."""
    sql = """
        SELECT i.id, i.source, i.ingested_at, i.metadata_json
        FROM items i
        LEFT JOIN signal_outcomes so ON so.item_id = i.id AND so.horizon_days = ?
        WHERE i.source IN ('fred', 'cboe', 'cftc')
          AND i.triage_decision = 'keep'
          AND i.ingested_at <= datetime('now', ?)
          AND so.id IS NULL
          AND json_extract(i.metadata_json, '$.z_score') IS NOT NULL
        ORDER BY i.ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (horizon_days, f"-{horizon_days} days", limit)).fetchall()


def get_followup_z(
    source: str, meta_key: str, key_value: str, after_iso: str
) -> float | None:
    """Latest z_score for same series/symbol/contract ingested after a given timestamp."""
    if meta_key not in _VALID_OUTCOME_KEYS:
        raise ValueError(f"Invalid meta_key: {meta_key!r}")
    sql = f"""
        SELECT CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score
        FROM items
        WHERE source = ?
          AND json_extract(metadata_json, '$.{meta_key}') = ?
          AND ingested_at > ?
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        ORDER BY ingested_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (source, key_value, after_iso)).fetchone()
    return float(row["z_score"]) if row else None


def upsert_outcome(
    item_id: int,
    horizon_days: int,
    outcome: str,
    original_z: float | None,
    followup_z: float | None,
    magnitude: float | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO signal_outcomes
               (item_id, horizon_days, outcome, original_z, followup_z, magnitude)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, horizon_days, outcome, original_z, followup_z, magnitude),
        )


def get_outcomes(item_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Return outcome rows keyed by item_id (7-day horizon, most recent check)."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    sql = f"""
        SELECT item_id, outcome, original_z, followup_z, magnitude
        FROM signal_outcomes
        WHERE item_id IN ({placeholders})
          AND horizon_days = 7
        ORDER BY checked_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(item_ids)).fetchall()
    seen: set[int] = set()
    result: dict[int, sqlite3.Row] = {}
    for row in rows:
        iid = row["item_id"]
        if iid not in seen:
            result[iid] = row
            seen.add(iid)
    return result


def items_ready_for_summary(
    limit: int | None = 75,
    source: str | None = None,
    per_source_cap: int | None = None,
) -> list[sqlite3.Row]:
    """Items that passed triage but haven't been summarized yet.

    When per_source_cap is set (and source filter is not), uses a SQLite window
    function (ROW_NUMBER OVER PARTITION BY source) so no single source can claim
    more than per_source_cap slots out of the overall limit.

    Args:
        limit: total max rows returned.
        source: optional source filter; when set, per_source_cap is ignored.
        per_source_cap: max items from any one source (ignored when source is set).
    """
    params: list = []

    if source is not None or per_source_cap is None:
        # Simple path: single-source filter or no per-source cap needed.
        sql = """
            SELECT id, source, source_id, url, title, author, content,
                   published_at, metadata_json, topic, triage_score
            FROM items
            WHERE triage_decision = 'keep'
              AND summary IS NULL
        """
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY triage_score DESC, ingested_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
    else:
        # Window-function path: cap each source independently, then take top-N overall.
        sql = """
            SELECT id, source, source_id, url, title, author, content,
                   published_at, metadata_json, topic, triage_score
            FROM (
                SELECT id, source, source_id, url, title, author, content,
                       published_at, ingested_at, metadata_json, topic, triage_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY source
                           ORDER BY triage_score DESC, ingested_at DESC
                       ) AS rn
                FROM items
                WHERE triage_decision = 'keep'
                  AND summary IS NULL
            )
            WHERE rn <= ?
            ORDER BY triage_score DESC, ingested_at DESC
        """
        params.append(per_source_cap)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def auto_keep_clipped() -> int:
    """Mark every untriaged clipped item as kept with score=1.0.

    Clips reach this state by virtue of the user's act of clipping — they've
    already self-triaged. We bypass Qwen and shove them straight to the
    summarizer. Returns the number of rows updated.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 1.0,
            triaged_at      = datetime('now')
        WHERE source = 'clipped'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


# Quantitative ingestors pre-filter to anomalous readings only — every item
# that reaches the DB has already passed a z-score or dollar threshold.
# Letting Qwen re-gate them with prose-oriented criteria drops valid signals.
QUANT_SOURCES = ("fred", "cboe", "cftc", "yahoo", "insider", "ftd", "collision", "industry_research")


def auto_keep_insurer_filings(
    tickers: set[str],
    form_types: set[str],
) -> int:
    """Auto-keep untriaged EDGAR items where ticker is in `tickers` AND
    metadata_json.form is in `form_types`. Returns rows updated.

    Implements the Wave 1 Python-side mandatory auto-keep: a model misread
    of source/form cannot drop a material insurer disclosure.

    Topic is locked at triage time per form (insurer filings should not be
    re-classified by the summarizer):
      8-K, 10-Q, 10-K → underwriting_results (carrier P&L / loss-cost signal)
      13F-HR           → ma_capital
    """
    if not tickers or not form_types:
        return 0
    placeholders = ",".join("?" * len(tickers))
    form_placeholders = ",".join("?" * len(form_types))
    sql = f"""
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.95,
            topic           = CASE json_extract(metadata_json, '$.form')
                                WHEN '13F-HR' THEN 'ma_capital'
                                ELSE 'underwriting_results'
                              END,
            triaged_at      = datetime('now')
        WHERE source = 'edgar'
          AND triage_decision IS NULL
          AND json_extract(metadata_json, '$.ticker') IN ({placeholders})
          AND json_extract(metadata_json, '$.form')   IN ({form_placeholders})
    """
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(tickers) + tuple(form_types))
        return cur.rowcount or 0


def auto_keep_quantitative() -> int:
    """Auto-keep untriaged items from quantitative ingestors.

    Applies topic_hint from metadata_json directly as the topic so items
    land in the right section of the daily note without Qwen guessing.
    Returns the number of rows updated.
    """
    placeholders = ",".join("?" * len(QUANT_SOURCES))
    sql = f"""
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.85,
            topic           = COALESCE(
                                json_extract(metadata_json, '$.topic_hint'),
                                'other'
                              ),
            triaged_at      = datetime('now')
        WHERE source IN ({placeholders})
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql, QUANT_SOURCES)
        return cur.rowcount or 0


def auto_keep_nhc_advisories() -> int:
    """Auto-keep all untriaged NHC items with score=1.0, topic=cat_event.

    The NHC ingestor only emits when a storm has a U.S./Caribbean threat,
    so every item that reaches the DB is material — equivalent 1.3× weight
    to EDGAR 8-K per the source-multiplier design decision.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 1.0,
            topic           = 'cat_event',
            triaged_at      = datetime('now')
        WHERE source = 'nhc'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def auto_keep_usgs_major() -> int:
    """Auto-keep untriaged USGS earthquakes M≥6.0 with score=0.95, topic=cat_event.

    The triage prompt already lists M≥6.0 as in-prompt auto-keep; this Python
    hook guarantees Ollama can't silently drop a major earthquake.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.95,
            topic           = 'cat_event',
            triaged_at      = datetime('now')
        WHERE source = 'usgs'
          AND triage_decision IS NULL
          AND CAST(json_extract(metadata_json, '$.magnitude') AS REAL) >= 6.0
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def auto_keep_courtlistener_dockets() -> int:
    """Auto-keep untriaged CourtListener dockets with score=0.85, topic=social_inflation.

    CourtListener items are pre-filtered to P&C-relevant nature-of-suit codes
    in the ingestor; every item that reaches the DB is a candidate MDL/mass-tort
    filing worth keeping without Ollama re-gating.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.85,
            topic           = 'social_inflation',
            triaged_at      = datetime('now')
        WHERE source = 'courtlistener'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def auto_keep_state_doi() -> int:
    """Auto-keep untriaged state DOI items with score=0.9, topic=regulatory_rate.

    The state_doi ingestor only emits items from enabled states with confirmed
    selectors; every item that reaches the DB is a direct DOI press release
    that warrants regulatory_rate classification without Ollama re-gating.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.9,
            topic           = 'regulatory_rate',
            triaged_at      = datetime('now')
        WHERE source = 'state_doi'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def update_triage(
    item_id: int,
    decision: str,        # 'keep' or 'drop'
    score: float,
    topic: str | None,
    burden_direction: str | None = None,
    burden_intensity: str | None = None,
) -> None:
    """Record a triage outcome on an item.

    burden_direction / burden_intensity are populated by the LLM for
    regulatory_rate items only (Regulatory Sonar lite). Pass None for all
    other topics — the columns will remain NULL.
    """
    pair: tuple[str, str] | None = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            pair = (row["source"], row["source_id"])
        conn.execute(
            """
            UPDATE items
            SET triage_decision  = ?,
                triage_score     = ?,
                topic            = ?,
                burden_direction = ?,
                burden_intensity = ?,
                triaged_at       = datetime('now')
            WHERE id = ?
            """,
            (decision, score, topic, burden_direction, burden_intensity, item_id),
        )
    if pair:
        sink.write_triage(pair[0], pair[1], {
            "decision":         decision,
            "score":            score,
            "topic":             topic,
            "burden_direction": burden_direction,
            "burden_intensity": burden_intensity,
        })


def update_summary(
    item_id: int,
    topic: str,
    summary: str,
    why_it_matters: str,
    confidence: str,
    see_also: list[str] | None,
) -> None:
    """Record summarizer output on an item."""
    see_also_json = json.dumps(see_also or [])
    pair: tuple[str, str] | None = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            pair = (row["source"], row["source_id"])
        conn.execute(
            """
            UPDATE items
            SET topic          = ?,
                summary        = ?,
                why_it_matters = ?,
                confidence     = ?,
                see_also       = ?,
                summarized_at  = datetime('now')
            WHERE id = ?
            """,
            (topic, summary, why_it_matters, confidence, see_also_json, item_id),
        )
    if pair:
        sink.write_summary(pair[0], pair[1], {
            "summary":        summary,
            "why_it_matters": why_it_matters,
            "see_also":       see_also_json,
            "confidence":     confidence,
            # materiality is set separately via update_materiality; the sink
            # accepts it as nullable. model_id/duration come from log_summarizer.
        })


def log_summarizer(
    backend: str,
    item_id: int,
    duration_ms: int,
    input_chars: int,
    output_chars: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append a row to summarizer_log for cost/usage tracking."""
    pair: tuple[str, str] | None = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            pair = (row["source"], row["source_id"])
        conn.execute(
            """
            INSERT INTO summarizer_log
                (backend, item_id, duration_ms, input_chars, output_chars, status, error)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (backend, item_id, duration_ms, input_chars, output_chars, status, error),
        )
    # Bronze telemetry — stage='summarize'. Source comes from the item.
    ended = datetime.now(timezone.utc)
    started = ended - timedelta(milliseconds=duration_ms)
    sink.write_telemetry({
        "run_id":       f"{started.isoformat(timespec='seconds')}-summarize-{item_id}",
        "stage":        "summarize",
        "source":       pair[0] if pair else None,
        "started_at":   started.isoformat(timespec="seconds"),
        "ended_at":     ended.isoformat(timespec="seconds"),
        "duration_ms":  duration_ms,
        "items_in":     input_chars,
        "items_out":    output_chars,
        "errors":       0 if status == "ok" else 1,
        "error_detail": error,
        "model_id":     backend,
    })


def triage_stats() -> dict[str, int]:
    """Counts grouped by triage_decision (incl. NULL = pending)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(triage_decision, 'pending') AS decision,
                   COUNT(*) AS n
            FROM items
            GROUP BY decision
            ORDER BY n DESC
            """
        ).fetchall()
    return {row["decision"]: row["n"] for row in rows}


def summarizer_stats(days: int = 7) -> dict[str, int]:
    """Recent summarizer activity by backend (for cost/budget tracking)."""
    sql = """
        SELECT backend, COUNT(*) AS n,
               SUM(input_chars)  AS in_chars,
               SUM(output_chars) AS out_chars
        FROM summarizer_log
        WHERE run_at >= datetime('now', ?)
        GROUP BY backend
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (f"-{days} days",)).fetchall()
    return {row["backend"]: dict(row) for row in rows}


# ── Phase 3 helpers (Obsidian publishing) ──────────────────────────────


def items_for_publish(date_iso: str) -> dict[str, list[sqlite3.Row]]:
    """Return everything to publish for a given calendar date (YYYY-MM-DD).

    Returns two lists:
      - 'summarized': triage=keep AND summary IS NOT NULL, ordered by topic + score
      - 'kept_unsummarized': triage=keep AND summary IS NULL (cap-overflow leftovers)

    Filters by ingested_at::date = date_iso to align with the daily note's date.
    """
    base = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, ingested_at, metadata_json,
               topic, summary, why_it_matters, confidence, see_also,
               triage_score, burden_direction, burden_intensity
        FROM items
        WHERE date(ingested_at) = date(?)
          AND triage_decision = 'keep'
    """
    with get_conn() as conn:
        summarized = conn.execute(
            base + " AND summary IS NOT NULL ORDER BY topic ASC, triage_score DESC",
            (date_iso,),
        ).fetchall()
        kept_unsum = conn.execute(
            base + " AND summary IS NULL ORDER BY triage_score DESC",
            (date_iso,),
        ).fetchall()
    return {"summarized": summarized, "kept_unsummarized": kept_unsum}


def items_by_topic(topic: str) -> list[sqlite3.Row]:
    """All summarized items for a topic, newest first. Used by topic archive writer."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at,
               summary, why_it_matters, confidence, see_also, triage_score
        FROM items
        WHERE topic = ?
          AND summary IS NOT NULL
        ORDER BY ingested_at DESC, id DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (topic,)).fetchall()


def topics_with_summaries() -> list[str]:
    """Distinct topics that have at least one summarized item."""
    sql = """
        SELECT DISTINCT topic
        FROM items
        WHERE topic IS NOT NULL AND summary IS NOT NULL
        ORDER BY topic
    """
    with get_conn() as conn:
        return [row["topic"] for row in conn.execute(sql).fetchall()]


def prune_past_events(days_grace: int = 1) -> int:
    """Delete calendar events whose date has passed (with a grace period). Returns count."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM upcoming_events WHERE event_date < date('now', ?)",
            (f"-{days_grace} days",),
        )
        return cur.rowcount or 0


def items_for_week(monday_iso: str, sunday_iso: str) -> list[sqlite3.Row]:
    """Summarized items ingested during a Mon–Sun week, sorted by triage score desc."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at, topic,
               summary, why_it_matters, confidence, see_also,
               triage_score, metadata_json, sentiment_label, entities_json
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        ORDER BY triage_score DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (monday_iso, sunday_iso)).fetchall()


def upsert_connections(date_iso: str, threads_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_connections (date, threads_json) VALUES (?, ?)",
            (date_iso, threads_json),
        )


def get_connections(date_iso: str) -> list:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT threads_json FROM daily_connections WHERE date = ?",
            (date_iso,),
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["threads_json"]) or []
    except (json.JSONDecodeError, KeyError):
        return []


def get_fred_signals_window(days: int = 45) -> list[sqlite3.Row]:
    """Latest z-score per FRED series from the past N days.

    Uses a window function to return only the most-recent reading per series,
    so the macro regime classifier always sees current values.
    """
    sql = """
        WITH ranked AS (
            SELECT
                json_extract(metadata_json, '$.series_id') AS series_id,
                CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score,
                ROW_NUMBER() OVER (
                    PARTITION BY json_extract(metadata_json, '$.series_id')
                    ORDER BY ingested_at DESC
                ) AS rn
            FROM items
            WHERE source = 'fred'
              AND ingested_at >= datetime('now', ?)
              AND json_extract(metadata_json, '$.series_id') IS NOT NULL
        )
        SELECT series_id, z_score FROM ranked WHERE rn = 1 ORDER BY series_id
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def upsert_regime(week_iso: str, regime: str, signals_json: str, narrative: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO macro_regime (week, regime, signals_json, narrative)
               VALUES (?, ?, ?, ?)""",
            (week_iso, regime, signals_json, narrative),
        )


def items_for_essay(start_iso: str, end_iso: str, limit: int = 40) -> list[sqlite3.Row]:
    """Top-scored kept items in a date range, returning raw content for the essay agent.

    Reads the content field (original source material), not AI-generated summaries,
    so the essay writer works from primary sources regardless of summarization status.
    """
    sql = """
        SELECT id, source, title, author, url, content,
               published_at, ingested_at, topic, triage_score, metadata_json
        FROM items
        WHERE triage_decision = 'keep'
          AND date(ingested_at) BETWEEN date(?) AND date(?)
          AND content IS NOT NULL
          AND content != ''
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (start_iso, end_iso, limit)).fetchall()


def connections_for_range(start_iso: str, end_iso: str) -> list[dict]:
    """All daily connection threads in a date range, newest first."""
    sql = """
        SELECT date, threads_json FROM daily_connections
        WHERE date BETWEEN ? AND ?
        ORDER BY date DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (start_iso, end_iso)).fetchall()
    result: list[dict] = []
    for row in rows:
        try:
            threads = json.loads(row["threads_json"]) or []
            result.extend(threads)
        except (json.JSONDecodeError, KeyError):
            pass
    return result


def get_latest_regime() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT week, regime, signals_json, narrative FROM macro_regime ORDER BY week DESC LIMIT 1"
        ).fetchone()


# ── Feature helpers ────────────────────────────────────────────────────


def items_needing_sentiment(limit: int = 200) -> list[sqlite3.Row]:
    sql = """
        SELECT id, title, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND sentiment_label IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_sentiment(item_id: int, label: str, score: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET sentiment_label = ?, sentiment_score = ? WHERE id = ?",
            (label, score, item_id),
        )


def items_needing_entities(limit: int = 500) -> list[sqlite3.Row]:
    sql = """
        SELECT id, title, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND entities_json IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_entities(item_id: int, entities_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET entities_json = ? WHERE id = ?",
            (entities_json, item_id),
        )


def upsert_events(events: list[dict]) -> None:
    sql = """
        INSERT OR IGNORE INTO upcoming_events
            (event_type, event_date, title, symbol, metadata_json)
        VALUES
            (:event_type, :event_date, :title, :symbol, :metadata_json)
    """
    with get_conn() as conn:
        for ev in events:
            conn.execute(sql, ev)


def get_upcoming_events(days_ahead: int = 90) -> list[sqlite3.Row]:
    sql = """
        SELECT event_type, event_date, title, symbol, metadata_json
        FROM upcoming_events
        WHERE event_date >= date('now')
          AND event_date <= date('now', ?)
        ORDER BY event_date ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"+{days_ahead} days",)).fetchall()


def top_items_for_cluster(
    cluster_id: str, start_iso: str, end_iso: str, limit: int = 3
) -> list[sqlite3.Row]:
    """Return top-scored items for a cluster within a date range."""
    sql = """
        SELECT id, title, source, published_at, ingested_at, triage_score, url
        FROM items
        WHERE triage_decision = 'keep'
          AND cluster_id = ?
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (cluster_id, start_iso, end_iso, limit)).fetchall()


def cluster_counts_for_range(start_iso: str, end_iso: str) -> dict[str, int]:
    sql = """
        SELECT cluster_id, COUNT(*) AS n
        FROM items
        WHERE triage_decision = 'keep'
          AND cluster_id IS NOT NULL
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        GROUP BY cluster_id
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (start_iso, end_iso)).fetchall()
    return {row["cluster_id"]: row["n"] for row in rows}


def get_fred_values_window(days: int = 90) -> list[sqlite3.Row]:
    """FRED latest_value readings per series per day for correlation analysis."""
    sql = """
        SELECT date(ingested_at) AS day,
               json_extract(metadata_json, '$.series_id') AS series_id,
               CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score
        FROM items
        WHERE source = 'fred'
          AND triage_decision = 'keep'
          AND ingested_at >= datetime('now', ?)
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        ORDER BY day ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def get_yahoo_pct_window(days: int = 90) -> list[sqlite3.Row]:
    """Yahoo daily pct_change readings per ticker for correlation analysis."""
    sql = """
        WITH ranked AS (
            SELECT date(ingested_at) AS day,
                   json_extract(metadata_json, '$.ticker') AS ticker,
                   CAST(json_extract(metadata_json, '$.pct_change') AS REAL) AS pct_change,
                   ROW_NUMBER() OVER (
                       PARTITION BY date(ingested_at), json_extract(metadata_json, '$.ticker')
                       ORDER BY ingested_at DESC
                   ) AS rn
            FROM items
            WHERE source = 'yahoo'
              AND triage_decision = 'keep'
              AND ingested_at >= datetime('now', ?)
              AND json_extract(metadata_json, '$.pct_change') IS NOT NULL
        )
        SELECT day, ticker, pct_change FROM ranked WHERE rn = 1 ORDER BY day ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def mark_published(item_ids: list[int]) -> None:
    """Stamp obsidian_written_at on items so we know they've been written.

    Note: this is informational only. The writer is idempotent and uses
    file-level state for de-duplication, not this column.
    """
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    sql = f"""
        UPDATE items
        SET obsidian_written_at = datetime('now')
        WHERE id IN ({placeholders})
    """
    with get_conn() as conn:
        conn.execute(sql, tuple(item_ids))


# ── Wave 2: PC regime detector (two-axis: market_cycle × cat_load) ────


def upsert_regime_signal(
    as_of: str,
    market_cycle: str,
    cat_load: str,
    market_cycle_mult: float,
    cat_load_mult: float,
    multiplier: float,
    evidence_json: str,
    source: str = "detector",
) -> int:
    """Insert a new regime_signals row. Returns rowid."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO regime_signals
                 (as_of, market_cycle, cat_load,
                  market_cycle_mult, cat_load_mult, multiplier,
                  evidence_json, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (as_of, market_cycle, cat_load,
             market_cycle_mult, cat_load_mult, multiplier,
             evidence_json, source),
        )
        rowid = cur.lastrowid or 0
    sink.write_regime({
        "as_of":             as_of,
        "market_cycle":      market_cycle,
        "cat_load":          cat_load,
        "market_cycle_mult": market_cycle_mult,
        "cat_load_mult":     cat_load_mult,
        "multiplier":        multiplier,
        "evidence_json":     evidence_json,
        "source":            source,
    })
    return rowid


def latest_regime_signal() -> sqlite3.Row | None:
    """Most recent regime_signals row, or None if none computed yet."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM regime_signals ORDER BY as_of DESC LIMIT 1"
        ).fetchone()


def recent_regime_signals(n: int = 3) -> list[sqlite3.Row]:
    """Most recent N regime_signals rows for hysteresis comparison."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM regime_signals ORDER BY as_of DESC LIMIT ?",
            (n,),
        ).fetchall()


def cat_load_counts(
    storm_window_days: int = 14,
    quake_window_days: int = 30,
) -> dict[str, int]:
    """Counts that feed the CAT-load detector.

    Returns dict with:
      - active_nhc: distinct NHC items ingested in last `storm_window_days`
      - recent_major_eq: USGS items M ≥ 6 ingested in last `quake_window_days`
      - recent_wildfire: NIFC items in last `storm_window_days`
    """
    with get_conn() as conn:
        active_nhc = conn.execute(
            "SELECT COUNT(*) FROM items WHERE source='nhc' "
            "AND ingested_at >= datetime('now', ?)",
            (f"-{storm_window_days} days",),
        ).fetchone()[0]
        recent_major_eq = conn.execute(
            "SELECT COUNT(*) FROM items WHERE source='usgs' "
            "AND ingested_at >= datetime('now', ?) "
            "AND CAST(json_extract(metadata_json, '$.magnitude') AS REAL) >= 6.0",
            (f"-{quake_window_days} days",),
        ).fetchone()[0]
        recent_wildfire = conn.execute(
            "SELECT COUNT(*) FROM items WHERE source='nifc' "
            "AND ingested_at >= datetime('now', ?)",
            (f"-{storm_window_days} days",),
        ).fetchone()[0]
    return {
        "active_nhc":      int(active_nhc or 0),
        "recent_major_eq": int(recent_major_eq or 0),
        "recent_wildfire": int(recent_wildfire or 0),
    }


def items_for_market_cycle(window_days: int = 60, limit: int = 80) -> list[sqlite3.Row]:
    """Trailing-window summarized items in underwriting_results + reinsurance_cycle.

    Used as evidence by the market_cycle LLM judgment call.
    """
    sql = """
        SELECT id, source, title, published_at, ingested_at,
               topic, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND topic IN ('underwriting_results', 'reinsurance_cycle')
          AND ingested_at >= datetime('now', ?)
        ORDER BY ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{window_days} days", limit)).fetchall()


# ── Wave 2: signal leaderboard ────────────────────────────────────────


def update_materiality(item_id: int, score: float | None) -> None:
    """Persist the materiality_score from summarize on an item."""
    if score is None:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET materiality_score = ? WHERE id = ?",
            (float(score), item_id),
        )


def upsert_signal_scores(rows: list[dict]) -> int:
    """Batch-insert a list of {item_id, score, source_mult, regime_mult, ...}.

    PRIMARY KEY is (item_id, computed_at); pass distinct computed_at values per
    batch to keep history. Returns row count inserted.
    """
    if not rows:
        return 0
    sql = """
        INSERT OR REPLACE INTO signal_scores
            (item_id, computed_at, score,
             source_mult, regime_mult, topic_relevance, recency,
             llm_judgment, topic_boost, burden_boost,
             insurer_boost, inflation_boost, regulatory_boost)
        VALUES
            (:item_id, :computed_at, :score,
             :source_mult, :regime_mult, :topic_relevance, :recency,
             :llm_judgment, :topic_boost, :burden_boost,
             :insurer_boost, :inflation_boost, :regulatory_boost)
    """
    item_ids = [int(r["item_id"]) for r in rows if r.get("item_id") is not None]
    src_map: dict[int, tuple[str, str]] = {}
    with get_conn() as conn:
        if item_ids:
            placeholders = ",".join("?" * len(item_ids))
            cur = conn.execute(
                f"SELECT id, source, source_id FROM items WHERE id IN ({placeholders})",
                item_ids,
            )
            for r in cur.fetchall():
                src_map[int(r["id"])] = (r["source"], r["source_id"])
        n = 0
        for r in rows:
            cur = conn.execute(sql, r)
            n += cur.rowcount or 0
    # Silver sink — one write per scored row, with all 10 boost factors.
    for r in rows:
        pair = src_map.get(int(r.get("item_id") or 0))
        if pair:
            sink.write_score(pair[0], pair[1], r)
    return n


def top_signal_scores(
    limit: int = 5,
    since_iso: str | None = None,
    source_filter: str | None = None,
) -> list[sqlite3.Row]:
    """Top-N items by latest score, optionally filtered by ingest date / source.

    Joins items so callers can render titles/summaries directly.
    """
    clauses = ["i.triage_decision = 'keep'", "i.summary IS NOT NULL"]
    params: list = []
    if since_iso:
        clauses.append("i.ingested_at >= ?")
        params.append(since_iso)
    if source_filter:
        clauses.append("i.source = ?")
        params.append(source_filter)
    where_sql = " AND ".join(clauses)

    sql = f"""
        WITH latest AS (
            SELECT item_id, MAX(computed_at) AS computed_at
            FROM signal_scores
            GROUP BY item_id
        )
        SELECT i.id, i.source, i.title, i.url, i.author, i.published_at,
               i.topic, i.summary, i.why_it_matters, i.confidence,
               i.see_also, i.triage_score, i.materiality_score,
               s.score, s.source_mult, s.regime_mult,
               s.topic_relevance, s.recency, s.llm_judgment,
               s.topic_boost, s.burden_boost
        FROM signal_scores s
        JOIN latest l ON s.item_id = l.item_id AND s.computed_at = l.computed_at
        JOIN items   i ON i.id = s.item_id
        WHERE {where_sql}
        ORDER BY s.score DESC, i.ingested_at DESC
        LIMIT ?
    """
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def signal_quality_by_source(since_iso: str | None = None) -> list[sqlite3.Row]:
    """Per-source aggregate: avg score, item count, since `since_iso` (or all-time).

    Used in the weekly note's 'which feeds earned their keep' table.
    """
    clauses = ["i.triage_decision = 'keep'", "i.summary IS NOT NULL"]
    params: list = []
    if since_iso:
        clauses.append("i.ingested_at >= ?")
        params.append(since_iso)
    where_sql = " AND ".join(clauses)
    sql = f"""
        WITH latest AS (
            SELECT item_id, MAX(computed_at) AS computed_at
            FROM signal_scores
            GROUP BY item_id
        )
        SELECT i.source           AS source,
               COUNT(*)            AS n,
               AVG(s.score)        AS avg_score,
               MAX(s.score)        AS max_score
        FROM signal_scores s
        JOIN latest l ON s.item_id = l.item_id AND s.computed_at = l.computed_at
        JOIN items   i ON i.id = s.item_id
        WHERE {where_sql}
        GROUP BY i.source
        ORDER BY avg_score DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()
