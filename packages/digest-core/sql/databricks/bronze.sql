-- Bronze layer — raw firehose. Hoard everything, including triage drops, full
-- FRED series, and operational telemetry. Replays + threshold re-tuning depend
-- on the raw signal still being here.
--
-- Shared-catalog model: one catalog (`digest`), domain-prefixed schemas. This
-- file is PC Digest's bronze (`pc_bronze`); macro-ai-digest ships its own
-- `macro_bronze` (sql/databricks/ in that repo). The sink's DATABRICKS_SCHEMA_PREFIX
-- (pc_ here) must match these schema names.
--
-- Join key across all medallion layers: item_hash = sha256(source || '::' ||
-- source_id), derived at sink-write time. SQLite stays untouched.
--
-- Apply with: USE CATALOG digest; then run this file. Schema `pc_bronze` is
-- created if missing. (Migration from the pre-prefix layout: the old `bronze`
-- schema can be dropped, or copied via CREATE TABLE pc_bronze.x AS SELECT * FROM bronze.x.)

CREATE SCHEMA IF NOT EXISTS pc_bronze;

-- Every IngestedItem from every source, INCLUDING items that get dropped at
-- triage. Drops are not waste — they train the triage-quality dashboards.
CREATE TABLE IF NOT EXISTS pc_bronze.ingested_items (
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
    CONSTRAINT pc_bronze_ingested_items_pk PRIMARY KEY (item_hash)
)
USING DELTA
PARTITIONED BY (source);

-- Semantic-layer embeddings (Option 3): one vector per item (title + summary).
-- Stored as JSON today (Free Edition); promote vector_json → ARRAY<FLOAT> + a
-- Delta Sync Index when moving to native Databricks Vector Search.
CREATE TABLE IF NOT EXISTS pc_bronze.item_embeddings (
    item_hash    STRING  NOT NULL,
    model        STRING  NOT NULL,
    dim          INT     NOT NULL,
    vector_json  STRING  NOT NULL,
    computed_at  TIMESTAMP NOT NULL,
    CONSTRAINT pc_bronze_item_embeddings_pk PRIMARY KEY (item_hash)
)
USING DELTA;

-- Full monthly FRED observations, not just the ±1.5σ anomalies that pass the
-- ingest gate. Storing the whole series lets us re-tune the z-score threshold
-- against history.
CREATE TABLE IF NOT EXISTS pc_bronze.fred_observations (
    series_id         STRING NOT NULL,
    observation_date  DATE   NOT NULL,
    value             DOUBLE,
    mom_pct_change    DOUBLE,
    yoy_pct_change    DOUBLE,
    zscore_12m        DOUBLE,         -- vs trailing-12m baseline
    is_anomaly        BOOLEAN,        -- |z| >= settings.fred_zscore_threshold
    fetched_at        TIMESTAMP NOT NULL,
    CONSTRAINT pc_bronze_fred_pk PRIMARY KEY (series_id, observation_date)
)
USING DELTA
PARTITIONED BY (series_id);

-- Loss triangles (Option 5): cumulative paid/incurred by accident year ×
-- development period, from naic_schedp / investor_supp. The chain-ladder
-- reserving estimates derived from these land in pc_silver.reserving_signals.
CREATE TABLE IF NOT EXISTS pc_bronze.loss_triangles (
    insurer          STRING NOT NULL,
    lob              STRING NOT NULL,           -- line of business
    metric           STRING NOT NULL,           -- 'paid' | 'incurred'
    accident_year    INT    NOT NULL,
    dev_period       INT    NOT NULL,
    cumulative_value DOUBLE NOT NULL,
    as_of            TIMESTAMP NOT NULL,
    CONSTRAINT pc_bronze_triangles_pk PRIMARY KEY (insurer, lob, metric, accident_year, dev_period, as_of)
)
USING DELTA
PARTITIONED BY (insurer);

-- Regime detector outputs over time. PC Digest is two-axis: market_cycle ×
-- cat_load. Mirrors the SQLite regime_signals table. `source` defaults to
-- 'detector' in the application layer (db.upsert_regime_signal), not via
-- column DEFAULT — Databricks Free Edition doesn't auto-enable the Delta
-- allowColumnDefaults feature.
CREATE TABLE IF NOT EXISTS pc_bronze.regime_signals (
    as_of              TIMESTAMP NOT NULL,
    market_cycle       STRING    NOT NULL,
    cat_load           STRING    NOT NULL,
    market_cycle_mult  DOUBLE    NOT NULL,
    cat_load_mult      DOUBLE    NOT NULL,
    multiplier         DOUBLE    NOT NULL,
    evidence_json      STRING,
    source             STRING    NOT NULL,
    CONSTRAINT pc_bronze_regime_pk PRIMARY KEY (as_of, source)
)
USING DELTA;

-- Operational telemetry per pipeline stage. Spots pipeline degradation,
-- enables source-level SLO dashboards. Subsumes SQLite run_log + summarizer_log.
-- `errors` defaulting to 0 is handled in the sink wiring, not via column DEFAULT.
CREATE TABLE IF NOT EXISTS pc_bronze.pipeline_telemetry (
    run_id        STRING NOT NULL,    -- UUID per pipeline invocation
    stage         STRING NOT NULL,    -- ingest|triage|summarize|publish|signals
    source        STRING,             -- nullable for non-source stages
    started_at    TIMESTAMP NOT NULL,
    ended_at      TIMESTAMP NOT NULL,
    duration_ms   BIGINT NOT NULL,
    items_in      INT,
    items_out     INT,
    errors        INT NOT NULL,
    error_detail  STRING,
    model_id      STRING,             -- for triage/summarize stages
    CONSTRAINT pc_bronze_telemetry_pk PRIMARY KEY (run_id, stage, source)
)
USING DELTA
PARTITIONED BY (stage);
