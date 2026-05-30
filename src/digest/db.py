"""SQLite schema and helpers. Raw sqlite3 — no ORM, keeps things boring.

The domain-agnostic base (items/run_log/summarizer_log schema, connection
management, and the generic CRUD helpers) lives in `digest_core.db`. This
module is the PC-domain layer on top: it owns the PC-specific migrations
(regime, signal scores, sonar, etc.), the auto-keep hooks, and the thin
wrappers that default `db_path` from settings and fan out to the Databricks
`sink`. Public signatures here are unchanged across the digest-core lift.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from digest_core.db import helpers as core_db

from digest.config import settings
from digest.sinks import sink

logger = logging.getLogger(__name__)

# Domain migrations layered on digest_core's BASE_SCHEMA (items / run_log /
# summarizer_log). Applied idempotently by core_db.init_db_with_migrations,
# which swallows "duplicate column" errors — so the ALTERs that re-add columns
# already present in BASE_SCHEMA (confidence, see_also, triage_*, summarized_at)
# are no-ops on a fresh DB and real migrations on a pre-lift one.
MIGRATIONS = [
    # fred_baseline predates the lift and is PC-only (FRED anomaly z-score
    # baselines), so it moved out of the shared base schema into this list.
    """CREATE TABLE IF NOT EXISTS fred_baseline (
        series_id       TEXT PRIMARY KEY,
        mean_delta      REAL,
        stddev_delta    REAL,
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
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
    # Wave 3 Phase 2 (2026-05-25): persist triage sub_tags (JSON list of strings,
    # e.g. ["litigation_tplf"]) + dedicated TPLF leaderboard boost.
    "ALTER TABLE items ADD COLUMN sub_tags TEXT DEFAULT '[]'",
    "ALTER TABLE signal_scores ADD COLUMN tplf_boost REAL DEFAULT 1.0",
    # Conviction tier (high/medium/low) derived from the leaderboard score.
    # Persisted so it flows to Databricks silver.signal_scores for analytics.
    "ALTER TABLE signal_scores ADD COLUMN tier TEXT",
    # Option 5: reserve-deterioration boost (1.0 neutral until reserving data).
    "ALTER TABLE signal_scores ADD COLUMN reserve_boost REAL DEFAULT 1.0",
    # Option 4: learned relevance score persisted alongside the heuristic
    # (NULL until a model is trained; ranking stays on the heuristic `score`).
    "ALTER TABLE signal_scores ADD COLUMN learned_score REAL",
    # Calibration loop (Databricks Option 1): the user's manual rating of an item,
    # the input to gold.score_calibration (system score vs. what the user values).
    # Keyed by (item_id, rated_at) to keep a history of re-ratings.
    """CREATE TABLE IF NOT EXISTS manual_ratings (
        item_id     INTEGER NOT NULL,
        rated_at    TEXT NOT NULL DEFAULT (datetime('now')),
        user_rating REAL NOT NULL,
        note        TEXT,
        PRIMARY KEY (item_id, rated_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_manual_ratings_item ON manual_ratings(item_id)",
    # Semantic layer (Databricks Option 3): one embedding vector per item
    # (title + summary), cached here + mirrored to pc_bronze.item_embeddings.
    # Powers `digest related`, semantic dedup, and `digest ask`.
    """CREATE TABLE IF NOT EXISTS item_embeddings (
        item_id     INTEGER PRIMARY KEY,
        model       TEXT NOT NULL,
        dim         INTEGER NOT NULL,
        vector_json TEXT NOT NULL,
        computed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Outcome backtest (Databricks Option 1b): did a top-ranked item actually
    # matter? One row per (item, horizon) — binary `corroborated` + which signals
    # fired (followon / edgar / regime / manual / stock_move). Distinct table from
    # the dead macro `signal_outcomes` substrate (z-score shape) kept for the
    # future "Signal" feature. Feeds gold.outcome_hit_rate + the Option-4 labels.
    """CREATE TABLE IF NOT EXISTS outcome_backtest (
        item_id         INTEGER NOT NULL,
        horizon_days    INTEGER NOT NULL,
        checked_at      TEXT NOT NULL DEFAULT (datetime('now')),
        corroborated    INTEGER NOT NULL,
        signals_json    TEXT NOT NULL DEFAULT '[]',
        followon_count  INTEGER DEFAULT 0,
        edgar_filed     INTEGER DEFAULT 0,
        regime_shifted  INTEGER DEFAULT 0,
        manual_rating   REAL,
        stock_move_z    REAL,
        stock_move_band TEXT,
        PRIMARY KEY (item_id, horizon_days)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_backtest_horizon ON outcome_backtest(horizon_days)",
    # Learned scorer (Databricks Option 4): a numpy logistic-regression relevance
    # model trained on the boost factors + heuristic score to predict
    # corroboration (the Option 1b labels). The model registry + per-item learned
    # scores; runs ALONGSIDE the heuristic (which stays authoritative).
    """CREATE TABLE IF NOT EXISTS learned_models (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        target              TEXT NOT NULL,
        horizon_days        INTEGER,
        trained_at          TEXT NOT NULL DEFAULT (datetime('now')),
        n_samples           INTEGER,
        auc                 REAL,
        heuristic_precision REAL,
        learned_precision   REAL,
        features_json       TEXT NOT NULL,
        model_json          TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS learned_scores (
        item_id       INTEGER NOT NULL,
        model_id      INTEGER NOT NULL,
        learned_score REAL NOT NULL,
        scored_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (item_id, model_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_learned_scores_item ON learned_scores(item_id)",
    # Reserving quant (Databricks Option 5): loss triangles (cumulative paid/
    # incurred by accident year × development period) from naic_schedp /
    # investor_supp, + the chain-ladder estimates derived from them.
    """CREATE TABLE IF NOT EXISTS loss_triangles (
        insurer          TEXT NOT NULL,
        lob              TEXT NOT NULL,
        metric           TEXT NOT NULL,           -- 'paid' | 'incurred'
        accident_year    INTEGER NOT NULL,
        dev_period       INTEGER NOT NULL,        -- development lag, years
        cumulative_value REAL NOT NULL,
        as_of            TEXT NOT NULL,
        PRIMARY KEY (insurer, lob, metric, accident_year, dev_period, as_of)
    )""",
    """CREATE TABLE IF NOT EXISTS reserving_signals (
        insurer          TEXT NOT NULL,
        lob              TEXT NOT NULL,
        metric           TEXT NOT NULL,
        as_of            TEXT NOT NULL,
        ultimate         REAL,
        latest           REAL,
        ibnr             REAL,
        prior_ibnr       REAL,
        deterioration_pct REAL,                   -- (ibnr - prior_ibnr)/prior_ibnr
        direction        TEXT,                    -- 'adverse' | 'favorable' | 'flat'
        PRIMARY KEY (insurer, lob, metric, as_of)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reserving_insurer ON reserving_signals(insurer)",
    # Lead 2 — CAT-Load Nowcast: federal-disaster / drought velocity feeding the
    # regime cat_load axis (local mirror of pc_bronze.cat_load_nowcast).
    """CREATE TABLE IF NOT EXISTS cat_load_nowcast (
        metric_name      TEXT NOT NULL,           -- 'open_disaster_declarations' | 'drought_coverage_pct'
        region           TEXT NOT NULL,           -- state code or 'US'
        observation_date TEXT NOT NULL,           -- month bucket (YYYY-MM-01) or ISO date
        value            REAL,
        zscore_12m       REAL,
        is_anomaly       INTEGER,                 -- 0/1
        source           TEXT,                    -- 'openfema' | 'usdm'
        fetched_at       TEXT,
        PRIMARY KEY (metric_name, region, observation_date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cat_nowcast_metric ON cat_load_nowcast(metric_name, region)",
    # Lead 1 — Reinsurance Pulse: priced ROL / ILS-spread series feeding the
    # regime market_cycle axis (local mirror of pc_bronze.reinsurance_pricing).
    """CREATE TABLE IF NOT EXISTS reinsurance_pricing (
        index_name       TEXT NOT NULL,            -- 'guycarp_us_property_cat_rol' | 'artemis_ils_spread'
        observation_date TEXT NOT NULL,
        value            REAL,                     -- ROL index level or spread (bps)
        zscore_12m       REAL,
        trend            TEXT,                     -- 'firming' | 'softening' | 'flat'
        is_anomaly       INTEGER,                  -- 0/1
        segment          TEXT,                     -- 'us_property_cat' | 'retro' | …
        source           TEXT,                     -- 'guycarp' | 'artemis' | 'lane'
        fetched_at       TEXT,
        PRIMARY KEY (index_name, observation_date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reins_pricing_index ON reinsurance_pricing(index_name)",
]


def init_db(db_path: Path | None = None) -> None:
    """Create DB file + base schema if missing; apply PC migrations idempotently."""
    core_db.init_db_with_migrations(db_path or settings.db_path, MIGRATIONS)


def get_conn(db_path: Path | None = None) -> AbstractContextManager[sqlite3.Connection]:
    """Connection context manager (row factory + WAL); defaults to the configured DB."""
    return core_db.get_conn(db_path or settings.db_path)


def upsert_items(items: Iterable["IngestedItem"]) -> int:  # noqa: F821
    """Insert new items, ignore duplicates. Returns count of new rows."""
    items_list = list(items)
    if not items_list:
        return 0
    with get_conn() as conn:
        inserted = core_db.upsert_items(conn, items_list)
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
    with get_conn() as conn:
        core_db.log_run(
            conn, run_type, source, items_fetched, items_new, duration_ms, status, error
        )
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
        return core_db.item_stats(conn)


def recent_items(source: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """Return most recently ingested items, optionally filtered by source."""
    with get_conn() as conn:
        return core_db.recent_items(conn, source, limit)


# Re-exported from digest_core so callers can keep importing it from digest.db.
utcnow_iso = core_db.utcnow_iso


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
               burden_direction, burden_intensity, sub_tags, metadata_json,
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
    with get_conn() as conn:
        return core_db.recent_kept_titles(conn, hours)


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
QUANT_SOURCES = ("fred", "collision", "industry_research")


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


_US_STATE_NAMES = frozenset({
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
})
_US_STATE_ABBREVS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
})
_US_TERRITORY_SUBSTRINGS = (
    "puerto rico", "virgin islands", "guam", "american samoa",
    "mariana islands",
)


def _is_us_place(place: str | None) -> bool:
    """Match USGS-format place strings naming a U.S. state or territory.

    USGS uses "29 km ENE of Calama, Chile" (non-US), "10 km W of Petrolia, CA"
    or "70 km SE of Cape Yakataga, Alaska" (US), and bare "Puerto Rico region"
    (territory). Trailing comma-segment matches state name/abbrev; territory
    tokens checked as case-insensitive substrings.
    """
    if not place:
        return False
    last = place.rsplit(",", 1)[-1].strip()
    if last in _US_STATE_NAMES or last in _US_STATE_ABBREVS:
        return True
    p_lower = place.lower()
    return any(t in p_lower for t in _US_TERRITORY_SUBSTRINGS)


def auto_keep_usgs_major() -> int:
    """Auto-keep untriaged USGS earthquakes M≥6.0 in U.S. places, score=0.95.

    Non-U.S. quakes (Chile, Indonesia, etc.) are intentionally not auto-kept
    so Ollama sees them and drops them per the triage prompt's U.S.-only
    cat_event rule. Raw ingest of all M≥5.0 events remains in bronze for
    later re-gating.
    """
    select_sql = """
        SELECT id, json_extract(metadata_json, '$.place') AS place
        FROM items
        WHERE source = 'usgs'
          AND triage_decision IS NULL
          AND CAST(json_extract(metadata_json, '$.magnitude') AS REAL) >= 6.0
    """
    update_sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.95,
            topic           = 'cat_event',
            triaged_at      = datetime('now')
        WHERE id = ?
    """
    kept = 0
    with get_conn() as conn:
        rows = conn.execute(select_sql).fetchall()
        for row in rows:
            if not _is_us_place(row["place"]):
                continue
            conn.execute(update_sql, (row["id"],))
            kept += 1
    return kept


def auto_keep_courtlistener_dockets() -> int:
    """Auto-keep untriaged CourtListener dockets, topic=social_inflation.

    Score depends on the metadata.mdl_match field set by the ingestor when the
    docket's case_name matched a configured mass-tort MDL keyword:
      - 0.95 when mdl_match is non-null (asbestos, PFAS, Roundup, opioid, etc.)
      - 0.85 otherwise (P&C-relevant nature-of-suit only, no MDL keyword)

    Every item that reaches the DB has already passed the ingestor's NOS filter,
    so it's worth keeping without Ollama re-gating.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = CASE
                                WHEN json_extract(metadata_json, '$.mdl_match') IS NOT NULL THEN 0.95
                                ELSE 0.85
                              END,
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


def auto_keep_investor_supp() -> int:
    """Auto-keep untriaged investor-supplement table items, topic=reserving.

    The investor_supp ingestor only emits rows from publicly-posted quarterly
    supplemental data; every item that reaches the DB is a parsed table from
    a known IR PDF and warrants reserving classification without re-gating.
    Score 0.85 (quarterly disclosure tier).
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.85,
            topic           = 'reserving',
            triaged_at      = datetime('now')
        WHERE source = 'investor_supp'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def auto_keep_naic_schedp() -> int:
    """Auto-keep untriaged NAIC Schedule P items, topic=reserving.

    Score 0.95 — Schedule P triangles are uniquely valuable annual adverse-
    development data; only emitted when a real data source is wired in
    (the ingestor no-ops while config/naic_schedp_sources.yaml is empty).
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.95,
            topic           = 'reserving',
            triaged_at      = datetime('now')
        WHERE source = 'naic_schedp'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


def auto_keep_serff() -> int:
    """Auto-keep untriaged SERFF rate filings, topic=regulatory_rate.

    Score depends on the requested rate-change magnitude stored in
    metadata.rate_change_pct (absolute value):
      - 0.95 when |Δ| >= 10%   (top-tier — sweeping price actions)
      - 0.9  otherwise (>= 5%, the ingestor's emit threshold)

    SERFF filings only reach the DB after passing the ingestor's >=5% filter
    and LOB whitelist, so they're worth keeping without Ollama re-gating.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = CASE
                                WHEN ABS(COALESCE(CAST(json_extract(metadata_json, '$.rate_change_pct') AS REAL), 0)) >= 10 THEN 0.95
                                ELSE 0.9
                              END,
            topic           = 'regulatory_rate',
            triaged_at      = datetime('now')
        WHERE source = 'serff'
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
    sub_tags: list[str] | None = None,
    source: str | None = None,
    source_id: str | None = None,
) -> None:
    """Record a triage outcome on an item.

    `source` / `source_id` are passed by callers that already hold the row
    (saves a SELECT for the sink's item_hash derivation). They fall back to
    a lookup when omitted, keeping older callers working.
    """
    sub_tags_json = json.dumps(sub_tags or [])
    pair: tuple[str, str] | None = (source, source_id) if (source and source_id) else None
    with get_conn() as conn:
        if pair is None:
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
                sub_tags         = ?,
                triaged_at       = datetime('now')
            WHERE id = ?
            """,
            (decision, score, topic, burden_direction, burden_intensity,
             sub_tags_json, item_id),
        )
    if pair:
        sink.write_triage(pair[0], pair[1], {
            "decision":         decision,
            "score":            score,
            "topic":             topic,
            "sub_tags":         sub_tags or [],
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
    source: str | None = None,
    source_id: str | None = None,
) -> None:
    """Record summarizer output on an item.

    `source` / `source_id` short-circuit the lookup the sink does for
    item_hash derivation when the caller already has them.
    """
    see_also_json = json.dumps(see_also or [])
    pair: tuple[str, str] | None = (source, source_id) if (source and source_id) else None
    with get_conn() as conn:
        if pair is None:
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
    source: str | None = None,
    source_id: str | None = None,
) -> None:
    """Append a row to summarizer_log for cost/usage tracking.

    `source` / `source_id` short-circuit the lookup the sink does for
    item_hash derivation when the caller already has them.
    """
    pair: tuple[str, str] | None = (source, source_id) if (source and source_id) else None
    with get_conn() as conn:
        if pair is None:
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
             insurer_boost, inflation_boost, regulatory_boost, tplf_boost, tier,
             reserve_boost, learned_score)
        VALUES
            (:item_id, :computed_at, :score,
             :source_mult, :regime_mult, :topic_relevance, :recency,
             :llm_judgment, :topic_boost, :burden_boost,
             :insurer_boost, :inflation_boost, :regulatory_boost, :tplf_boost, :tier,
             :reserve_boost, :learned_score)
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
            # Default the newer optional columns so callers (older scripts/tests)
            # that omit them still bind cleanly.
            r.setdefault("reserve_boost", 1.0)
            r.setdefault("learned_score", None)
            cur = conn.execute(sql, r)
            n += cur.rowcount or 0
    # Silver sink — one write per scored row, with all 10 boost factors.
    for r in rows:
        pair = src_map.get(int(r.get("item_id") or 0))
        if pair:
            sink.write_score(pair[0], pair[1], r)
    return n


def upsert_manual_rating(
    item_id: int,
    user_rating: float,
    note: str | None = None,
    rated_at: str | None = None,
) -> None:
    """Record the user's manual rating (1.0-5.0) of an item — the calibration
    input behind gold.score_calibration. Keyed by (item_id, rated_at) so
    re-rating keeps history. Fans out to silver.manual_ratings.
    """
    rated_at = rated_at or utcnow_iso()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO manual_ratings (item_id, rated_at, user_rating, note)
               VALUES (?, ?, ?, ?)""",
            (item_id, rated_at, user_rating, note),
        )
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    if row:
        sink.write_rating(
            row["source"], row["source_id"],
            {"user_rating": user_rating, "note": note, "rated_at": rated_at},
        )


def recent_manual_ratings(limit: int = 50) -> list[sqlite3.Row]:
    """Most recent manual ratings joined to their item title — for review/calibration."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.item_id, r.user_rating, r.note, r.rated_at,
                      i.title, i.topic, i.source
               FROM manual_ratings r
               JOIN items i ON i.id = r.item_id
               ORDER BY r.rated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def calibration_rows(limit: int = 50) -> list[sqlite3.Row]:
    """Latest manual rating per item joined to its latest computed score — the
    local mirror of gold.score_calibration (system score vs. what the user
    valued). system_score is NULL for items that were rated but never scored.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            WITH latest_rating AS (
                SELECT item_id, user_rating, note, rated_at,
                       ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY rated_at DESC) rn
                FROM manual_ratings
            ),
            latest_score AS (
                SELECT item_id, score,
                       ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY computed_at DESC) rn
                FROM signal_scores
            )
            SELECT r.item_id, i.title, i.topic, i.source,
                   r.user_rating, r.rated_at, r.note,
                   s.score AS system_score
            FROM latest_rating r
            JOIN items i ON i.id = r.item_id
            LEFT JOIN latest_score s ON s.item_id = r.item_id AND s.rn = 1
            WHERE r.rn = 1
            ORDER BY r.rated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


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


def brief_alerts(hours: int = 48) -> dict[str, list[sqlite3.Row]]:
    """Watch-worthy conditions for the daily brief, in one pass over SQLite —
    the local analog of the Databricks Alerts (Option 2):

    - high_burden: kept regulatory items flagged burden_intensity='high'
    - tplf:        kept items tagged litigation_tplf (nuclear-verdict / mass-tort)
    - fred:        recent FRED items (the ingestor only keeps ±σ anomalies)
    - degraded:    sources whose most-recent run errored
    """
    cutoff = f"-{hours} hours"
    with get_conn() as conn:
        high_burden = conn.execute(
            """SELECT id, title, source, burden_direction FROM items
               WHERE triage_decision = 'keep' AND burden_intensity = 'high'
                 AND triaged_at >= datetime('now', ?)
               ORDER BY triaged_at DESC LIMIT 10""",
            (cutoff,),
        ).fetchall()
        tplf = conn.execute(
            """SELECT id, title, source FROM items
               WHERE triage_decision = 'keep' AND sub_tags LIKE '%litigation_tplf%'
                 AND triaged_at >= datetime('now', ?)
               ORDER BY triaged_at DESC LIMIT 10""",
            (cutoff,),
        ).fetchall()
        fred = conn.execute(
            """SELECT id, title FROM items
               WHERE source = 'fred' AND ingested_at >= datetime('now', ?)
               ORDER BY ingested_at DESC LIMIT 10""",
            (cutoff,),
        ).fetchall()
        degraded = conn.execute(
            """SELECT source, status, error, run_at FROM run_log
               WHERE id IN (SELECT MAX(id) FROM run_log GROUP BY source)
                 AND status = 'error'
               ORDER BY run_at DESC""",
        ).fetchall()
    return {"high_burden": high_burden, "tplf": tplf, "fred": fred, "degraded": degraded}


def items_needing_embedding(limit: int = 500) -> list[sqlite3.Row]:
    """Kept items with no embedding yet — id + the text to embed (title + summary)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT i.id, i.title, i.summary
            FROM items i
            LEFT JOIN item_embeddings e ON e.item_id = i.id
            WHERE i.triage_decision = 'keep' AND e.item_id IS NULL
            ORDER BY i.ingested_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def upsert_embedding(item_id: int, model: str, vector: list[float]) -> None:
    """Persist one item's embedding (replace on re-embed); mirror to bronze."""
    vector_json = json.dumps([round(float(x), 6) for x in vector])
    dim = len(vector)
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO item_embeddings (item_id, model, dim, vector_json)
               VALUES (?, ?, ?, ?)""",
            (item_id, model, dim, vector_json),
        )
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    if row:
        sink.write_embedding(row["source"], row["source_id"], {
            "model": model, "dim": dim, "vector_json": vector_json,
        })


def load_embeddings() -> list[sqlite3.Row]:
    """All stored embeddings joined to item display fields, for local kNN."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT e.item_id, e.vector_json, i.title, i.topic, i.source, i.url
            FROM item_embeddings e
            JOIN items i ON i.id = e.item_id
            ORDER BY e.item_id
            """
        ).fetchall()


def get_items_text(ids: list[int]) -> dict[int, sqlite3.Row]:
    """id → row (title/summary/why/topic/source/url) for the retrieved RAG set."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, title, summary, why_it_matters, topic, source, url
                FROM items WHERE id IN ({placeholders})""",
            ids,
        ).fetchall()
    return {r["id"]: r for r in rows}


# ── Outcome backtest helpers (Option 1b) ─────────────────────────────────


def items_for_backtest(horizon_days: int, limit: int = 500) -> list[sqlite3.Row]:
    """Scored, kept items whose `horizon_days` window has fully elapsed and that
    have no outcome_backtest row yet at that horizon."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT i.id, i.source, i.source_id, i.title, i.summary, i.topic,
                   i.ingested_at
            FROM items i
            JOIN signal_scores s     ON s.item_id = i.id
            LEFT JOIN outcome_backtest o ON o.item_id = i.id AND o.horizon_days = ?
            WHERE i.triage_decision = 'keep'
              AND i.ingested_at <= datetime('now', ?)
              AND o.item_id IS NULL
            GROUP BY i.id
            ORDER BY i.ingested_at DESC
            LIMIT ?
            """,
            (horizon_days, f"-{horizon_days} days", limit),
        ).fetchall()


def embeddings_with_time() -> list[sqlite3.Row]:
    """Embeddings + ingested_at + topic — fuel for forward-window similarity."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.item_id, e.vector_json, i.ingested_at, i.topic
               FROM item_embeddings e JOIN items i ON i.id = e.item_id"""
        ).fetchall()


def forward_topic_count(topic: str | None, start_iso: str, end_iso: str,
                        exclude_id: int) -> int:
    """Kept items with the same topic ingested in (start, end] — followon fallback
    when embeddings are unavailable."""
    if not topic:
        return 0
    with get_conn() as conn:
        (n,) = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE triage_decision = 'keep' AND topic = ?
                 AND datetime(ingested_at) > datetime(?)
                 AND datetime(ingested_at) <= datetime(?) AND id <> ?""",
            (topic, start_iso, end_iso, exclude_id),
        ).fetchone()
    return n


def edgar_filings_in_window(ticker: str, start_iso: str, end_iso: str) -> int:
    """Count EDGAR filings for `ticker` ingested in (start, end] (source_id = TICKER:accession)."""
    with get_conn() as conn:
        (n,) = conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE source = 'edgar' AND source_id LIKE ?
                 AND datetime(ingested_at) > datetime(?)
                 AND datetime(ingested_at) <= datetime(?)""",
            (f"{ticker}:%", start_iso, end_iso),
        ).fetchone()
    return n


def regime_rows_in_window(start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    """regime_signals rows with as_of in (start, end]."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT as_of, market_cycle, cat_load FROM regime_signals
               WHERE datetime(as_of) > datetime(?) AND datetime(as_of) <= datetime(?)
               ORDER BY as_of""",
            (start_iso, end_iso),
        ).fetchall()


def regime_state_at(iso: str) -> sqlite3.Row | None:
    """Prevailing regime state at `iso` (latest row at/before it)."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT market_cycle, cat_load FROM regime_signals
               WHERE datetime(as_of) <= datetime(?) ORDER BY as_of DESC LIMIT 1""",
            (iso,),
        ).fetchone()


def manual_rating_for(item_id: int) -> float | None:
    """Highest manual rating recorded for an item, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(user_rating) AS r FROM manual_ratings WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    return row["r"] if row and row["r"] is not None else None


def upsert_backtest_outcome(item_id: int, horizon_days: int, outcome: dict) -> None:
    """Persist one (item, horizon) backtest outcome; mirror to silver.outcome_backtest."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO outcome_backtest
                 (item_id, horizon_days, checked_at, corroborated, signals_json,
                  followon_count, edgar_filed, regime_shifted, manual_rating,
                  stock_move_z, stock_move_band)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, horizon_days, utcnow_iso(),
             1 if outcome["corroborated"] else 0,
             json.dumps(outcome.get("signals", [])),
             outcome.get("followon_count", 0),
             1 if outcome.get("edgar_filed") else 0,
             1 if outcome.get("regime_shifted") else 0,
             outcome.get("manual_rating"),
             outcome.get("stock_move_z"),
             outcome.get("stock_move_band")),
        )
        row = conn.execute(
            "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    if row:
        sink.write_outcome(row["source"], row["source_id"], {
            "horizon_days":    horizon_days,
            "corroborated":    bool(outcome["corroborated"]),
            "signals":         outcome.get("signals", []),
            "followon_count":  outcome.get("followon_count", 0),
            "edgar_filed":     bool(outcome.get("edgar_filed")),
            "regime_shifted":  bool(outcome.get("regime_shifted")),
            "manual_rating":   outcome.get("manual_rating"),
            "stock_move_z":    outcome.get("stock_move_z"),
            "stock_move_band": outcome.get("stock_move_band"),
        })


# ── Learned scorer helpers (Option 4) ────────────────────────────────────

# Shared factor projection (latest signal_scores per item) used by both the
# training-set assembly and inference.
_LEARN_FACTORS = (
    "s.score, s.source_mult, s.regime_mult, s.topic_relevance, s.recency, "
    "s.llm_judgment, s.topic_boost, s.burden_boost, s.insurer_boost, "
    "s.inflation_boost, s.regulatory_boost, s.tplf_boost"
)


def learning_dataset(horizon_days: int) -> list[sqlite3.Row]:
    """Labeled rows for training: latest score factors + materiality + the
    corroboration label, for items with an outcome_backtest row at `horizon`."""
    with get_conn() as conn:
        return conn.execute(
            f"""
            WITH latest AS (
                SELECT item_id, MAX(computed_at) AS computed_at
                FROM signal_scores GROUP BY item_id
            )
            SELECT i.id AS item_id, i.materiality_score, {_LEARN_FACTORS},
                   o.corroborated
            FROM outcome_backtest o
            JOIN latest l        ON l.item_id = o.item_id
            JOIN signal_scores s ON s.item_id = l.item_id AND s.computed_at = l.computed_at
            JOIN items i         ON i.id = o.item_id
            WHERE o.horizon_days = ?
            """,
            (horizon_days,),
        ).fetchall()


def items_to_learn_score() -> list[sqlite3.Row]:
    """Latest-scored items to apply the learned model to (factors + materiality)."""
    with get_conn() as conn:
        return conn.execute(
            f"""
            WITH latest AS (
                SELECT item_id, MAX(computed_at) AS computed_at
                FROM signal_scores GROUP BY item_id
            )
            SELECT i.id AS item_id, i.source, i.source_id, i.materiality_score,
                   {_LEARN_FACTORS}
            FROM latest l
            JOIN signal_scores s ON s.item_id = l.item_id AND s.computed_at = l.computed_at
            JOIN items i         ON i.id = l.item_id
            """
        ).fetchall()


def save_learned_model(meta: dict) -> int:
    """Persist a trained model + its metrics; returns the new model_id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO learned_models
                 (target, horizon_days, n_samples, auc, heuristic_precision,
                  learned_precision, features_json, model_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (meta["target"], meta.get("horizon_days"), meta.get("n_samples"),
             meta.get("auc"), meta.get("heuristic_precision"),
             meta.get("learned_precision"), meta["features_json"], meta["model_json"]),
        )
        return cur.lastrowid


def latest_learned_model(target: str = "corroborated") -> sqlite3.Row | None:
    """Most recently trained model for a target, or None."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM learned_models WHERE target = ? ORDER BY id DESC LIMIT 1",
            (target,),
        ).fetchone()


def learned_model_by_id(model_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM learned_models WHERE id = ?", (model_id,)
        ).fetchone()


def upsert_learned_score(
    item_id: int, model_id: int, score: float,
    source: str | None = None, source_id: str | None = None,
) -> None:
    """Persist one item's learned score; mirror to silver.learned_scores."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO learned_scores (item_id, model_id, learned_score, scored_at)
               VALUES (?, ?, ?, ?)""",
            (item_id, model_id, score, utcnow_iso()),
        )
        if not (source and source_id):
            row = conn.execute(
                "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                source, source_id = row["source"], row["source_id"]
    if source and source_id:
        sink.write_learned_score(source, source_id, {
            "model_id": model_id, "learned_score": score,
        })


# ── Reserving quant helpers (Option 5) ───────────────────────────────────


def upsert_triangle_cells(cells: list[dict]) -> int:
    """Bulk-insert loss-triangle cells. Returns rows written."""
    if not cells:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO loss_triangles
                 (insurer, lob, metric, accident_year, dev_period, cumulative_value, as_of)
               VALUES (:insurer, :lob, :metric, :accident_year, :dev_period,
                       :cumulative_value, :as_of)""",
            cells,
        )
    return len(cells)


def load_triangle(insurer: str, lob: str, metric: str,
                  as_of: str | None = None) -> list[sqlite3.Row]:
    """Triangle cells for one insurer/LOB/metric (latest as_of if unspecified)."""
    with get_conn() as conn:
        if as_of is None:
            as_of_row = conn.execute(
                """SELECT MAX(as_of) AS a FROM loss_triangles
                   WHERE insurer=? AND lob=? AND metric=?""",
                (insurer, lob, metric),
            ).fetchone()
            as_of = as_of_row["a"] if as_of_row else None
        if as_of is None:
            return []
        return conn.execute(
            """SELECT accident_year, dev_period, cumulative_value FROM loss_triangles
               WHERE insurer=? AND lob=? AND metric=? AND as_of=?
               ORDER BY accident_year, dev_period""",
            (insurer, lob, metric, as_of),
        ).fetchall()


def triangle_keys() -> list[sqlite3.Row]:
    """Distinct (insurer, lob, metric) with their latest as_of — what to compute."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT insurer, lob, metric, MAX(as_of) AS as_of
               FROM loss_triangles GROUP BY insurer, lob, metric"""
        ).fetchall()


def prior_reserving_ibnr(insurer: str, lob: str, metric: str, before: str) -> float | None:
    """Most recent prior IBNR estimate (strictly before `before`) for deterioration."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT ibnr FROM reserving_signals
               WHERE insurer=? AND lob=? AND metric=? AND as_of < ?
               ORDER BY as_of DESC LIMIT 1""",
            (insurer, lob, metric, before),
        ).fetchone()
    return row["ibnr"] if row and row["ibnr"] is not None else None


def upsert_reserving_signal(sig: dict) -> None:
    """Persist a chain-ladder estimate; mirror to silver.reserving_signals."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reserving_signals
                 (insurer, lob, metric, as_of, ultimate, latest, ibnr, prior_ibnr,
                  deterioration_pct, direction)
               VALUES (:insurer, :lob, :metric, :as_of, :ultimate, :latest, :ibnr,
                       :prior_ibnr, :deterioration_pct, :direction)""",
            sig,
        )
    sink.write_reserving(sig)


def latest_reserving_signals(limit: int = 50) -> list[sqlite3.Row]:
    """Most recent reserving estimate per insurer/LOB/metric, for display."""
    with get_conn() as conn:
        return conn.execute(
            """
            WITH latest AS (
                SELECT insurer, lob, metric, MAX(as_of) AS as_of
                FROM reserving_signals GROUP BY insurer, lob, metric
            )
            SELECT r.* FROM reserving_signals r
            JOIN latest l ON r.insurer=l.insurer AND r.lob=l.lob
                         AND r.metric=l.metric AND r.as_of=l.as_of
            ORDER BY ABS(COALESCE(r.deterioration_pct, 0)) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def reserving_severity_map() -> dict[str, float]:
    """{insurer: worst adverse deterioration_pct} across latest signals — fuel for
    the (not-yet-wired) reserve_deterioration_boost."""
    out: dict[str, float] = {}
    for r in latest_reserving_signals(limit=500):
        if r["direction"] == "adverse" and r["deterioration_pct"]:
            out[r["insurer"]] = max(out.get(r["insurer"], 0.0), r["deterioration_pct"])
    return out


def upsert_cat_nowcast(rows: list[dict]) -> int:
    """Persist CAT-load nowcast observations; mirror to bronze.cat_load_nowcast.
    Returns rows written."""
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO cat_load_nowcast
                 (metric_name, region, observation_date, value, zscore_12m,
                  is_anomaly, source, fetched_at)
               VALUES (:metric_name, :region, :observation_date, :value,
                       :zscore_12m, :is_anomaly, :source, :fetched_at)""",
            rows,
        )
    sink.write_cat_load_nowcast(rows)
    return len(rows)


def latest_cat_nowcast(metric_name: str, region: str = "US") -> sqlite3.Row | None:
    """Newest nowcast observation for a metric/region, or None."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM cat_load_nowcast
               WHERE metric_name=? AND region=?
               ORDER BY observation_date DESC LIMIT 1""",
            (metric_name, region),
        ).fetchone()


def upsert_reinsurance_pricing(rows: list[dict]) -> int:
    """Persist reinsurance-pricing observations; mirror to bronze.reinsurance_pricing."""
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO reinsurance_pricing
                 (index_name, observation_date, value, zscore_12m, trend,
                  is_anomaly, segment, source, fetched_at)
               VALUES (:index_name, :observation_date, :value, :zscore_12m, :trend,
                       :is_anomaly, :segment, :source, :fetched_at)""",
            rows,
        )
    sink.write_reinsurance_pricing(rows)
    return len(rows)


def latest_reinsurance_pricing(index_name: str | None = None) -> sqlite3.Row | None:
    """Newest reinsurance-pricing observation overall, or for a given index."""
    with get_conn() as conn:
        if index_name:
            return conn.execute(
                """SELECT * FROM reinsurance_pricing WHERE index_name=?
                   ORDER BY observation_date DESC LIMIT 1""",
                (index_name,),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM reinsurance_pricing ORDER BY observation_date DESC LIMIT 1"
        ).fetchone()


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
