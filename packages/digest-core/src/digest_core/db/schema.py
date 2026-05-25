"""Base SQLite schema shared by digest-style projects.

Owns: `items`, `run_log`, `summarizer_log`. Domain projects add their own
tables via migration lists composed on top of `BASE_SCHEMA`.

This is the only schema digest_core owns. Domain projects are responsible
for their own additional tables (regime_signals, signal_scores, macro_regime,
etc.) and their own migration sequencing.
"""
from __future__ import annotations

BASE_SCHEMA = """
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

-- Phase 2 cost/usage tracking on the summarizer step.
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
