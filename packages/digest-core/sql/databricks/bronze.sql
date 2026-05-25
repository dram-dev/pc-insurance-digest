-- Bronze layer — raw firehose. Hoard everything, including triage drops, full
-- FRED series, and operational telemetry. Replays + threshold re-tuning depend
-- on the raw signal still being here.
--
-- Join key across all medallion layers: item_hash = sha256(source || '::' ||
-- source_id), derived at sink-write time. SQLite stays untouched.
--
-- Apply with: USE CATALOG <catalog>; then run this file. Schema `bronze` is
-- created if missing.

CREATE SCHEMA IF NOT EXISTS bronze;

-- Every IngestedItem from every source, INCLUDING items that get dropped at
-- triage. Drops are not waste — they train the triage-quality dashboards.
CREATE TABLE IF NOT EXISTS bronze.ingested_items (
    item_hash      STRING  NOT NULL,
    source         STRING  NOT NULL,
    source_id      STRING  NOT NULL,
    url            STRING,
    title          STRING  NOT NULL,
    author         STRING,
    content        STRING,
    published_at   TIMESTAMP,
    ingested_at    TIMESTAMP NOT NULL,
    metadata_json  STRING,            -- raw metadata as JSON
    topic_hint     STRING,            -- starting prior from feed config
    CONSTRAINT bronze_ingested_items_pk PRIMARY KEY (item_hash)
)
USING DELTA
PARTITIONED BY (source);

-- Full monthly FRED observations, not just the ±1.5σ anomalies that pass the
-- ingest gate. Storing the whole series lets us re-tune the z-score threshold
-- against history.
CREATE TABLE IF NOT EXISTS bronze.fred_observations (
    series_id         STRING NOT NULL,
    observation_date  DATE   NOT NULL,
    value             DOUBLE,
    mom_pct_change    DOUBLE,
    yoy_pct_change    DOUBLE,
    zscore_12m        DOUBLE,         -- vs trailing-12m baseline
    is_anomaly        BOOLEAN,        -- |z| >= settings.fred_zscore_threshold
    fetched_at        TIMESTAMP NOT NULL,
    CONSTRAINT bronze_fred_pk PRIMARY KEY (series_id, observation_date)
)
USING DELTA
PARTITIONED BY (series_id);

-- Regime detector outputs over time. PC Digest is two-axis: market_cycle ×
-- cat_load. Mirrors the SQLite regime_signals table.
CREATE TABLE IF NOT EXISTS bronze.regime_signals (
    as_of              TIMESTAMP NOT NULL,
    market_cycle       STRING    NOT NULL,
    cat_load           STRING    NOT NULL,
    market_cycle_mult  DOUBLE    NOT NULL,
    cat_load_mult      DOUBLE    NOT NULL,
    multiplier         DOUBLE    NOT NULL,
    evidence_json      STRING,
    source             STRING    NOT NULL DEFAULT 'detector',
    CONSTRAINT bronze_regime_pk PRIMARY KEY (as_of, source)
)
USING DELTA;

-- Operational telemetry per pipeline stage. Spots pipeline degradation,
-- enables source-level SLO dashboards. Subsumes SQLite run_log + summarizer_log.
CREATE TABLE IF NOT EXISTS bronze.pipeline_telemetry (
    run_id        STRING NOT NULL,    -- UUID per pipeline invocation
    stage         STRING NOT NULL,    -- ingest|triage|summarize|publish|signals
    source        STRING,             -- nullable for non-source stages
    started_at    TIMESTAMP NOT NULL,
    ended_at      TIMESTAMP NOT NULL,
    duration_ms   BIGINT NOT NULL,
    items_in      INT,
    items_out     INT,
    errors        INT NOT NULL DEFAULT 0,
    error_detail  STRING,
    model_id      STRING,             -- for triage/summarize stages
    CONSTRAINT bronze_telemetry_pk PRIMARY KEY (run_id, stage, source)
)
USING DELTA
PARTITIONED BY (stage);
