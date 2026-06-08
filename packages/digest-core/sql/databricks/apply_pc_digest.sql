-- Generated for the pc_digest catalog (unprefixed bronze/silver/gold schemas).
-- Apply in the Databricks SQL editor. Idempotent (CREATE IF NOT EXISTS / OR REPLACE).
USE CATALOG `pc_digest`;

-- ============ bronze.sql ============
-- Bronze layer — raw firehose. Hoard everything, including triage drops, full
-- FRED series, and operational telemetry. Replays + threshold re-tuning depend
-- on the raw signal still being here.
--
-- Shared-catalog model: one catalog (`digest`), domain-prefixed schemas. This
-- file is PC Digest's bronze (`bronze`); macro-ai-digest ships its own
-- `macro_bronze` (sql/databricks/ in that repo). The sink's DATABRICKS_SCHEMA_PREFIX
-- (pc_ here) must match these schema names.
--
-- Join key across all medallion layers: item_hash = sha256(source || '::' ||
-- source_id), derived at sink-write time. SQLite stays untouched.
--
-- Apply with: USE CATALOG digest; then run this file. Schema `bronze` is
-- created if missing. (Migration from the pre-prefix layout: the old `bronze`
-- schema can be dropped, or copied via CREATE TABLE bronze.x AS SELECT * FROM bronze.x.)

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

-- Semantic-layer embeddings (Option 3): one vector per item (title + summary).
-- Stored as JSON today (Free Edition); promote vector_json → ARRAY<FLOAT> + a
-- Delta Sync Index when moving to native Databricks Vector Search.
CREATE TABLE IF NOT EXISTS bronze.item_embeddings (
    item_hash    STRING  NOT NULL,
    model        STRING  NOT NULL,
    dim          INT     NOT NULL,
    vector_json  STRING  NOT NULL,
    computed_at  TIMESTAMP NOT NULL,
    CONSTRAINT bronze_item_embeddings_pk PRIMARY KEY (item_hash)
)
USING DELTA;

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

-- Loss triangles (Option 5): cumulative paid/incurred by accident year ×
-- development period, from naic_schedp / investor_supp. The chain-ladder
-- reserving estimates derived from these land in silver.reserving_signals.
CREATE TABLE IF NOT EXISTS bronze.loss_triangles (
    insurer          STRING NOT NULL,
    lob              STRING NOT NULL,           -- raw line of business (source-specific)
    metric           STRING NOT NULL,           -- 'paid' | 'incurred'
    accident_year    INT    NOT NULL,
    dev_period       INT    NOT NULL,
    cumulative_value DOUBLE NOT NULL,
    as_of            TIMESTAMP NOT NULL,
    canonical_lob    STRING,                     -- unified taxonomy (digest.parse.lob_canonical)
    CONSTRAINT bronze_triangles_pk PRIMARY KEY (insurer, lob, metric, accident_year, dev_period, as_of)
)
USING DELTA
PARTITIONED BY (insurer);

-- Regime detector outputs over time. PC Digest is two-axis: market_cycle ×
-- cat_load. Mirrors the SQLite regime_signals table. `source` defaults to
-- 'detector' in the application layer (db.upsert_regime_signal), not via
-- column DEFAULT — Databricks Free Edition doesn't auto-enable the Delta
-- allowColumnDefaults feature.
CREATE TABLE IF NOT EXISTS bronze.regime_signals (
    as_of              TIMESTAMP NOT NULL,
    market_cycle       STRING    NOT NULL,
    cat_load           STRING    NOT NULL,
    market_cycle_mult  DOUBLE    NOT NULL,
    cat_load_mult      DOUBLE    NOT NULL,
    multiplier         DOUBLE    NOT NULL,
    evidence_json      STRING,
    source             STRING    NOT NULL,
    CONSTRAINT bronze_regime_pk PRIMARY KEY (as_of, source)
)
USING DELTA;

-- ── Wave 4 — Insurance EKG leads (vital-sign feeds) ──────────────────────
-- These three bronze tables mirror the fred_observations shape (raw value +
-- mom/yoy + trailing-z + anomaly flag + fetch timestamp). Ingestors are future
-- waves; the DatabricksSink already has a no-op write_* per table. See
-- docs/WAVE4_EKG_PLAN.md for the full lead-by-lead spec.

-- Lead 1 — Reinsurance Pulse. GuyCarp rate-on-line indices + Artemis/Lane ILS
-- spreads. Hardens regime.market_cycle (hard/soft cycle position). One row per
-- index per observation; ai_forecast() projects the next renewal's direction.
CREATE TABLE IF NOT EXISTS bronze.reinsurance_pricing (
    index_name        STRING NOT NULL,            -- 'guycarp_us_property_cat_rol' | 'artemis_ils_spread' | …
    observation_date  DATE   NOT NULL,
    value             DOUBLE,                       -- ROL index level or spread (bps)
    zscore_12m        DOUBLE,                        -- latest vs trailing baseline
    trend             STRING,                        -- 'firming' | 'softening' | 'flat' (reduce_series)
    is_anomaly        BOOLEAN,
    segment           STRING,                       -- 'us_property_cat' | 'retro' | 'casualty' | …
    source            STRING,                       -- 'guycarp' | 'artemis' | 'lane'
    fetched_at        TIMESTAMP NOT NULL,
    CONSTRAINT bronze_reins_pricing_pk PRIMARY KEY (index_name, observation_date)
)
USING DELTA
PARTITIONED BY (index_name);

-- Lead 2 — CAT-Load Nowcast. OpenFEMA disaster declarations + NOAA CPC seasonal
-- outlook + US Drought Monitor + PowerOutage.us. Hardens regime.cat_load
-- (low_season / active_season / post_major_event). Region-scoped so a state
-- nowcast and the national roll-up coexist; Lakeflow DLT + ai_forecast().
CREATE TABLE IF NOT EXISTS bronze.cat_load_nowcast (
    metric_name       STRING NOT NULL,            -- 'open_disaster_declarations' | 'cpc_above_normal_prob' | 'drought_coverage_pct' | 'customers_out'
    region            STRING NOT NULL,            -- state code or 'US'
    observation_date  DATE   NOT NULL,
    value             DOUBLE,
    mom_pct_change    DOUBLE,
    yoy_pct_change    DOUBLE,
    zscore_12m        DOUBLE,
    is_anomaly        BOOLEAN,
    source            STRING,                       -- 'openfema' | 'noaa_cpc' | 'usdm' | 'poweroutage'
    fetched_at        TIMESTAMP NOT NULL,
    CONSTRAINT bronze_cat_nowcast_pk PRIMARY KEY (metric_name, region, observation_date)
)
USING DELTA
PARTITIONED BY (metric_name);

-- Lead 3 — Severity Tape. Manheim Used Vehicle Value Index + the existing FRED
-- parts/labor/medical loss-cost series, unified so the inflation_*_boost has a
-- forward read. Feeds signals._inflation_keyword_boost calibration + Feature
-- Store; ai_forecast() on the index level.
CREATE TABLE IF NOT EXISTS bronze.severity_index (
    index_name        STRING NOT NULL,            -- 'manheim_uvvi' | 'fred_parts_ppi' | 'fred_body_labor' | 'fred_medical_cpi'
    observation_date  DATE   NOT NULL,
    value             DOUBLE,
    mom_pct_change    DOUBLE,
    yoy_pct_change    DOUBLE,
    zscore_12m        DOUBLE,
    is_anomaly        BOOLEAN,
    category          STRING,                       -- 'used_vehicle' | 'parts' | 'labor' | 'medical'
    source            STRING,                       -- 'manheim' | 'fred'
    fetched_at        TIMESTAMP NOT NULL,
    CONSTRAINT bronze_severity_pk PRIMARY KEY (index_name, observation_date)
)
USING DELTA
PARTITIONED BY (index_name);

-- Operational telemetry per pipeline stage. Spots pipeline degradation,
-- enables source-level SLO dashboards. Subsumes SQLite run_log + summarizer_log.
-- `errors` defaulting to 0 is handled in the sink wiring, not via column DEFAULT.
CREATE TABLE IF NOT EXISTS bronze.pipeline_telemetry (
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
    CONSTRAINT bronze_telemetry_pk PRIMARY KEY (run_id, stage, source)
)
USING DELTA
PARTITIONED BY (stage);

-- ============ silver.sql ============
-- Silver layer — cleansed and joined. Each row references a bronze item_hash;
-- separate tables for triage / score / summary so producers stay decoupled
-- (a re-score doesn't bump triage history, etc.).
--
-- PC Digest's silver (`silver`) in the shared `digest` catalog. Apply after
-- bronze (bronze) DDL.

CREATE SCHEMA IF NOT EXISTS silver;

-- Triage verdict per item per triage run. Drop_reason is the model's reason
-- string when decision='drop'; for keep items, `reason` holds the keep rationale.
CREATE TABLE IF NOT EXISTS silver.triage_verdicts (
    item_hash         STRING    NOT NULL,
    triaged_at        TIMESTAMP NOT NULL,
    decision          STRING    NOT NULL,        -- 'keep'|'drop'
    score             DOUBLE,                    -- 0.0-1.0, model's raw score
    topic             STRING,
    sub_tags          ARRAY<STRING>,
    confidence        STRING,                    -- 'high'|'medium'|'low'
    reason            STRING,                    -- ≤ 50 words
    burden_direction  STRING,                    -- 'increasing'|'neutral'|'decreasing' or null
    burden_intensity  STRING,                    -- 'high'|'medium'|'low' or null
    state             STRING,                    -- Wave 4 Lead 9: US state code for regulatory_rate items (null elsewhere)
    model_id          STRING,                    -- triage model identifier
    CONSTRAINT silver_triage_pk PRIMARY KEY (item_hash, triaged_at)
)
USING DELTA;
-- Lead 9 migration on an existing table:
--   ALTER TABLE silver.triage_verdicts ADD COLUMN state STRING;

-- Per-item leaderboard score with all 11 multiplicative factors broken out.
-- Enables back-testing: "if I bump PGR insurer_boost to 1.7×, what would last
-- week's top-5 have looked like?" Columns mirror SQLite signal_scores exactly.
CREATE TABLE IF NOT EXISTS silver.signal_scores (
    item_hash         STRING    NOT NULL,
    computed_at       TIMESTAMP NOT NULL,
    score             DOUBLE    NOT NULL,
    source_mult       DOUBLE,
    regime_mult       DOUBLE,
    topic_relevance   DOUBLE,
    recency           DOUBLE,
    llm_judgment      DOUBLE,
    topic_boost       DOUBLE,
    burden_boost      DOUBLE,
    insurer_boost     DOUBLE,
    inflation_boost   DOUBLE,
    regulatory_boost  DOUBLE,
    tplf_boost        DOUBLE,                      -- Wave 3 Phase 2: TPLF / mass-tort sub_tag boost
    tier              STRING,                       -- conviction tier (high/medium/low) from the score
    reserve_boost     DOUBLE,                       -- Option 5: adverse reserve development on a named insurer
    learned_score     DOUBLE,                       -- Option 4: learned relevance, alongside the heuristic
    CONSTRAINT silver_score_pk PRIMARY KEY (item_hash, computed_at)
)
USING DELTA;

-- Summarizer output + materiality. Stub-summarized items (EDGAR / FRED short-
-- circuits in summarize.py) also land here, with model_id set to 'stub'.
CREATE TABLE IF NOT EXISTS silver.summaries (
    item_hash       STRING    NOT NULL,
    summarized_at   TIMESTAMP NOT NULL,
    summary         STRING    NOT NULL,
    why_it_matters  STRING,
    see_also        STRING,
    materiality     DOUBLE,                      -- 0.0-1.5, sharpened anchors
    confidence      STRING,
    input_chars     INT,
    output_chars    INT,
    model_id        STRING,                      -- 'mlx:Qwen3.5-27B' or 'stub'
    duration_ms     BIGINT,
    CONSTRAINT silver_summaries_pk PRIMARY KEY (item_hash, summarized_at)
)
USING DELTA;

-- Manual ratings from `digest rate` / Obsidian _meta/Score Higher.md. Populated
-- by the calibration loop (Databricks Option 1a); gold.score_calibration
-- left-joins this so it gracefully degrades to no-output when empty.
CREATE TABLE IF NOT EXISTS silver.manual_ratings (
    item_hash   STRING    NOT NULL,
    rated_at    TIMESTAMP NOT NULL,
    user_rating DOUBLE,                          -- 1.0-5.0, user's target score
    note        STRING,                          -- freeform context from the user
    CONSTRAINT silver_manual_pk PRIMARY KEY (item_hash, rated_at)
)
USING DELTA;

-- Outcome backtest (Option 1b): did a ranked item actually matter, N days on?
-- One row per (item, horizon ∈ {7,30}); corroborated = any of 5 signals fired.
-- Feeds gold.outcome_hit_rate + the Option-4 learned scorer's training labels.
CREATE TABLE IF NOT EXISTS silver.outcome_backtest (
    item_hash       STRING    NOT NULL,
    horizon_days    INT       NOT NULL,
    checked_at      TIMESTAMP NOT NULL,
    corroborated    BOOLEAN   NOT NULL,
    signals         ARRAY<STRING>,               -- which fired: followon/edgar/regime/manual/stock_move
    followon_count  INT,
    edgar_filed     BOOLEAN,
    regime_shifted  BOOLEAN,
    manual_rating   DOUBLE,
    stock_move_z    DOUBLE,                       -- signed σ of the insurer's return
    stock_move_band STRING,                       -- 0.5/0.75/1.0/1.25/1.5/1.75/2.0/2+
    CONSTRAINT silver_backtest_pk PRIMARY KEY (item_hash, horizon_days)
)
USING DELTA;

-- Learned relevance score (Option 4): the numpy logistic-regression model's
-- predicted P(corroborated), written alongside the heuristic so gold can A/B
-- them. model_id references the SQLite learned_models registry.
CREATE TABLE IF NOT EXISTS silver.learned_scores (
    item_hash     STRING    NOT NULL,
    model_id      INT       NOT NULL,
    learned_score DOUBLE    NOT NULL,
    scored_at     TIMESTAMP NOT NULL,
    CONSTRAINT silver_learned_pk PRIMARY KEY (item_hash, model_id)
)
USING DELTA;

-- Reserving estimates (Option 5): chain-ladder ultimate / IBNR per insurer /
-- LOB / metric, with deterioration vs. the prior estimate. Insurer-keyed (a
-- derived actuarial fact, not a news item).
CREATE TABLE IF NOT EXISTS silver.reserving_signals (
    insurer           STRING    NOT NULL,
    lob               STRING    NOT NULL,
    metric            STRING    NOT NULL,
    as_of             TIMESTAMP NOT NULL,
    ultimate          DOUBLE,
    latest            DOUBLE,
    ibnr              DOUBLE,
    prior_ibnr        DOUBLE,
    deterioration_pct DOUBLE,                    -- (ibnr - prior_ibnr) / prior_ibnr
    direction         STRING,                    -- 'adverse' | 'favorable' | 'flat'
    CONSTRAINT silver_reserving_pk PRIMARY KEY (insurer, lob, metric, as_of)
)
USING DELTA;

-- Component-level insurer XBRL facts (concept registry — datasets 1-13).
CREATE TABLE IF NOT EXISTS silver.insurer_xbrl_facts (
    fact_key         STRING NOT NULL,
    insurer          STRING NOT NULL,
    dataset          STRING NOT NULL,
    concept          STRING NOT NULL,
    field            STRING,
    period_end       STRING,
    period_type      STRING,
    accident_year    INT,
    segment          STRING,
    product          STRING,
    subsegment       STRING,
    geography        STRING,
    investment_type  STRING,
    instrument       STRING,
    fv_level         STRING,
    value            DOUBLE NOT NULL,
    is_count         INT,
    as_of            STRING,
    CONSTRAINT silver_xbrl_facts_pk PRIMARY KEY (fact_key)
)
USING DELTA;

-- Statutory high-level facts for the big mutuals (NAIC InsData + free III).
CREATE TABLE IF NOT EXISTS silver.statutory_facts (
    fact_key      STRING NOT NULL,
    insurer       STRING NOT NULL,
    source        STRING NOT NULL,
    dataset       STRING NOT NULL,
    field         STRING,
    line          STRING,
    accident_year INT,
    period        STRING,
    value         DOUBLE NOT NULL,
    unit          STRING,
    as_of         STRING,
    canonical_lob STRING,
    CONSTRAINT silver_statutory_pk PRIMARY KEY (fact_key)
)
USING DELTA;

-- ── Wave 4 — Insurance EKG leads (derived facts) ─────────────────────────
-- See docs/WAVE4_EKG_PLAN.md. Ingestors are future waves; the DatabricksSink
-- already has a no-op write_* per table.

-- Lead 4 — Litigation Pressure Index. Marathon nuclear-verdict tracker +
-- Westfleet TPLF survey + CourtListener docket velocity, rolled to a
-- per-state × sector pressure index. Hardens signals.litigation_tplf_boost.
-- (state, sector)-keyed — a derived legal-environment fact, not a news item.
CREATE TABLE IF NOT EXISTS silver.litigation_pressure (
    state            STRING    NOT NULL,           -- US state code, or 'US' for the national roll-up
    sector           STRING    NOT NULL,           -- 'commercial_auto' | 'product_liability' | 'med_mal' | …
    as_of            TIMESTAMP NOT NULL,
    verdict_count    INT,                           -- nuclear verdicts (≥$10M) in the window
    median_award     DOUBLE,                        -- USD
    tplf_commitments DOUBLE,                        -- disclosed third-party funding committed, USD
    docket_velocity  DOUBLE,                        -- new P&C dockets / day, trailing window
    pressure_index   DOUBLE,                        -- composite 0-100, drives the boost calibration
    CONSTRAINT silver_litigation_pk PRIMARY KEY (state, sector, as_of)
)
USING DELTA;

-- Lead 5 — Disclosure Sentiment. Reserve-tone NLP (FinBERT / Loughran-McDonald,
-- or ai_query() on the warehouse) over EDGAR MD&A / reserve footnotes. Hardens
-- signals reserve_deterioration_boost with a *language* read that leads the
-- chain-ladder number. (insurer, period)-keyed.
CREATE TABLE IF NOT EXISTS silver.disclosure_sentiment (
    insurer               STRING    NOT NULL,       -- ticker
    period                STRING    NOT NULL,       -- filing period, e.g. '2026Q1'
    as_of                 TIMESTAMP NOT NULL,        -- filing date
    reserve_tone          STRING,                    -- 'strengthening' | 'releasing' | 'neutral'
    adverse_language_score DOUBLE,                   -- 0.0-1.0, higher = more adverse framing
    source_filing         STRING,                    -- accession number or filing URL
    CONSTRAINT silver_disclosure_pk PRIMARY KEY (insurer, period, as_of)
)
USING DELTA;

-- Lead 8 — InsurTech Capital-Flow. Structured extraction (ai_query() on the
-- warehouse / Ollama locally) of funding-round + broker-M&A news into deal
-- facts. Powers the ai_insurtech topic with substance so the 35% share cap
-- stops being the only governor. item_hash-keyed back to the source news item.
CREATE TABLE IF NOT EXISTS silver.capital_flows (
    item_hash   STRING    NOT NULL,                 -- joins to bronze.ingested_items
    as_of       TIMESTAMP NOT NULL,
    deal_type   STRING,                              -- 'funding_round' | 'm&a' | 'broker_acquisition' | 'ipo'
    amount_usd  DOUBLE,
    stage       STRING,                              -- 'seed' | 'series_a' | … | null for M&A
    target      STRING,                              -- company acquired / funded
    investors   STRING,                              -- JSON array or comma-list of investors / acquirer
    CONSTRAINT silver_capital_flows_pk PRIMARY KEY (item_hash)
)
USING DELTA;

-- ============ gold.sql ============
-- Gold layer — curated views for read consumption. These are views, not
-- materialized tables, so they always reflect current bronze/silver state.
-- Promote any view to a materialized table (Lakeflow/DLT) if query latency
-- becomes a concern.
--
-- PC Digest's gold (`gold`) in the shared `digest` catalog. Apply after
-- bronze + silver DDL.

CREATE SCHEMA IF NOT EXISTS gold;

-- Latest-per-item score (one row per item_hash, most recent computed_at).
-- Most gold views need this; materialize it via a small helper view.
CREATE OR REPLACE VIEW gold.latest_scores AS
SELECT *
FROM (
    SELECT
        s.*,
        ROW_NUMBER() OVER (PARTITION BY item_hash ORDER BY computed_at DESC) AS rn
    FROM silver.signal_scores s
)
WHERE rn = 1;

-- Daily leaderboard — top items per day with full score breakdown.
CREATE OR REPLACE VIEW gold.daily_leaderboard AS
SELECT
    DATE(s.computed_at)                                            AS digest_date,
    ROW_NUMBER() OVER (PARTITION BY DATE(s.computed_at)
                       ORDER BY s.score DESC)                       AS rank,
    s.score,
    b.source,
    b.title,
    b.url,
    t.topic,
    t.sub_tags,
    t.confidence,
    t.burden_direction,
    t.burden_intensity,
    sm.materiality,
    s.source_mult, s.regime_mult, s.topic_relevance, s.recency,
    s.llm_judgment, s.topic_boost, s.burden_boost,
    s.insurer_boost, s.inflation_boost, s.regulatory_boost,
    s.item_hash
FROM gold.latest_scores s
JOIN silver.triage_verdicts t  USING (item_hash)
JOIN bronze.ingested_items b   USING (item_hash)
LEFT JOIN silver.summaries sm  USING (item_hash)
WHERE t.decision = 'keep';

-- Weekly leaderboard — same shape, weekly partition.
CREATE OR REPLACE VIEW gold.weekly_leaderboard AS
SELECT
    DATE_TRUNC('week', s.computed_at)                              AS week_start,
    ROW_NUMBER() OVER (PARTITION BY DATE_TRUNC('week', s.computed_at)
                       ORDER BY s.score DESC)                       AS rank,
    s.score,
    b.source,
    b.title,
    b.url,
    t.topic,
    sm.materiality,
    s.item_hash
FROM gold.latest_scores s
JOIN silver.triage_verdicts t  USING (item_hash)
JOIN bronze.ingested_items b   USING (item_hash)
LEFT JOIN silver.summaries sm  USING (item_hash)
WHERE t.decision = 'keep';

-- Per-source quality — keep rate, top drop reasons, avg materiality.
-- Surfaces noisy sources and validates the triage tightening from Wave 3 Phase 1.
CREATE OR REPLACE VIEW gold.source_quality AS
SELECT
    b.source,
    DATE(b.ingested_at)                                            AS day,
    COUNT(*)                                                        AS items_ingested,
    COUNT_IF(t.decision = 'keep')                                   AS items_kept,
    COUNT_IF(t.decision = 'drop')                                   AS items_dropped,
    ROUND(COUNT_IF(t.decision = 'keep') / NULLIF(COUNT(*), 0), 3)   AS keep_rate,
    AVG(CASE WHEN t.decision = 'keep' THEN sm.materiality END)      AS avg_materiality,
    AVG(CASE WHEN t.decision = 'keep' THEN s.score END)             AS avg_score
FROM bronze.ingested_items b
LEFT JOIN silver.triage_verdicts t  USING (item_hash)
LEFT JOIN silver.summaries sm       USING (item_hash)
LEFT JOIN gold.latest_scores s      USING (item_hash)
GROUP BY b.source, DATE(b.ingested_at);

-- Score calibration — system score vs the user's manual rating (digest rate /
-- Obsidian _meta). Live once silver.manual_ratings has data; gracefully
-- no-output before then.
CREATE OR REPLACE VIEW gold.score_calibration AS
SELECT
    m.item_hash,
    b.source,
    b.title,
    t.topic,
    m.user_rating,
    s.score                       AS system_score,
    s.score - m.user_rating       AS delta,
    m.rated_at,
    m.note
FROM silver.manual_ratings m
LEFT JOIN gold.latest_scores s   USING (item_hash)
LEFT JOIN silver.triage_verdicts t USING (item_hash)
LEFT JOIN bronze.ingested_items b  USING (item_hash);

-- Regime history with prevailing regime at any point in time.
CREATE OR REPLACE VIEW gold.regime_history AS
SELECT
    as_of,
    market_cycle,
    cat_load,
    market_cycle_mult,
    cat_load_mult,
    multiplier,
    source,
    evidence_json
FROM bronze.regime_signals
ORDER BY as_of DESC;

-- Operational SLOs by source + stage. Watch this when a feed silently degrades.
CREATE OR REPLACE VIEW gold.pipeline_slos AS
SELECT
    stage,
    source,
    DATE(started_at)                                               AS day,
    COUNT(*)                                                        AS runs,
    SUM(items_in)                                                   AS items_in,
    SUM(items_out)                                                  AS items_out,
    AVG(duration_ms)                                                AS avg_duration_ms,
    PERCENTILE(duration_ms, 0.95)                                   AS p95_duration_ms,
    SUM(errors)                                                     AS total_errors
FROM bronze.pipeline_telemetry
GROUP BY stage, source, DATE(started_at);

-- ── Option 2 analytics views (Genie-friendly) ───────────────────────────

-- Topic volume over time — kept items per topic per day. Powers "which topics
-- are heating up?" in Genie + a topic-trend dashboard tile.
CREATE OR REPLACE VIEW gold.topic_trend AS
SELECT
    DATE(t.triaged_at)                                             AS day,
    t.topic,
    COUNT(*)                                                        AS items_kept,
    AVG(sm.materiality)                                            AS avg_materiality,
    AVG(s.score)                                                   AS avg_score
FROM silver.triage_verdicts t
LEFT JOIN silver.summaries sm   USING (item_hash)
LEFT JOIN gold.latest_scores s  USING (item_hash)
WHERE t.decision = 'keep'
GROUP BY DATE(t.triaged_at), t.topic;

-- Regulatory burden trend — counts by intensity/direction over time, the
-- Regulatory Sonar surface in the lakehouse. Drives the burden Alert + a
-- "burden pressure" dashboard tile. (Per-state breakdown awaits a state field
-- captured at triage time; today this is intensity × direction.)
CREATE OR REPLACE VIEW gold.burden_trend AS
SELECT
    DATE(t.triaged_at)                                             AS day,
    t.burden_intensity,
    t.burden_direction,
    COUNT(*)                                                        AS items
FROM silver.triage_verdicts t
WHERE t.decision = 'keep'
  AND t.topic = 'regulatory_rate'
  AND t.burden_intensity IS NOT NULL
GROUP BY DATE(t.triaged_at), t.burden_intensity, t.burden_direction;

-- Lead 9 — Regulatory Burden Barometer, per state. Mirrors burden_trend but
-- grouped by the Wave 4 `state` column on triage_verdicts, so Genie can answer
-- "which states are tightening the screws?" and the per-state Alert can fire.
-- Rows only appear once triage starts populating `state` (null today → excluded).
CREATE OR REPLACE VIEW gold.burden_by_state AS
SELECT
    t.state,
    DATE(t.triaged_at)                                             AS day,
    t.burden_intensity,
    t.burden_direction,
    COUNT(*)                                                        AS items,
    -- intensity-weighted pressure: high=3, medium=2, low=1
    SUM(CASE t.burden_intensity WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2
                                WHEN 'low' THEN 1 ELSE 0 END)       AS burden_pressure
FROM silver.triage_verdicts t
WHERE t.decision = 'keep'
  AND t.topic = 'regulatory_rate'
  AND t.state IS NOT NULL
GROUP BY t.state, DATE(t.triaged_at), t.burden_intensity, t.burden_direction;

-- ── Option 1b: outcome calibration ───────────────────────────────────────

-- Top-N precision — of items ranked in the daily top-N, what fraction
-- corroborated at each horizon? The headline "is the leaderboard right?" metric.
CREATE OR REPLACE VIEW gold.outcome_hit_rate AS
WITH ranked AS (
    SELECT DATE(s.computed_at) AS day, s.item_hash,
           ROW_NUMBER() OVER (PARTITION BY DATE(s.computed_at)
                              ORDER BY s.score DESC) AS rnk
    FROM gold.latest_scores s
    JOIN silver.triage_verdicts t USING (item_hash)
    WHERE t.decision = 'keep'
)
SELECT r.day, o.horizon_days, COUNT(*) AS ranked_items,
       SUM(CASE WHEN o.corroborated THEN 1 ELSE 0 END) AS corroborated,
       ROUND(AVG(CASE WHEN o.corroborated THEN 1.0 ELSE 0.0 END), 3) AS hit_rate
FROM ranked r
JOIN silver.outcome_backtest o USING (item_hash)
WHERE r.rnk <= 5
GROUP BY r.day, o.horizon_days;

-- Corroboration rate by topic / source / tier — which slices the leaderboard
-- gets right vs. wrong. Directly drives scoring-weight tuning + is the Option-4
-- training signal. (Tier from latest_scores.)
CREATE OR REPLACE VIEW gold.outcome_by_factor AS
SELECT
    o.horizon_days,
    t.topic,
    b.source,
    s.tier,
    COUNT(*)                                                       AS n,
    ROUND(AVG(CASE WHEN o.corroborated THEN 1.0 ELSE 0.0 END), 3)  AS corroboration_rate
FROM silver.outcome_backtest o
JOIN silver.triage_verdicts t  USING (item_hash)
JOIN bronze.ingested_items b   USING (item_hash)
LEFT JOIN gold.latest_scores s USING (item_hash)
GROUP BY o.horizon_days, t.topic, b.source, s.tier;

-- ── Option 5: reserving ───────────────────────────────────────────────────

-- Latest chain-ladder estimate per insurer/LOB/metric, ranked by how adverse
-- the reserve development is. Feeds the reserving digest callout + (once wired)
-- the reserve_deterioration_boost.
CREATE OR REPLACE VIEW gold.reserving_signals AS
WITH latest AS (
    SELECT insurer, lob, metric, MAX(as_of) AS as_of
    FROM silver.reserving_signals GROUP BY insurer, lob, metric
)
SELECT r.insurer, r.lob, r.metric, r.as_of,
       r.ultimate, r.latest, r.ibnr, r.prior_ibnr,
       r.deterioration_pct, r.direction
FROM silver.reserving_signals r
JOIN latest l ON r.insurer = l.insurer AND r.lob = l.lob
             AND r.metric = l.metric AND r.as_of = l.as_of
ORDER BY ABS(COALESCE(r.deterioration_pct, 0)) DESC;

-- ── Wave 4 — Insurance EKG panel ──────────────────────────────────────────
-- gold.market_ekg: one row per lead = the panel of vital signs. Each arm
-- pulls its lead's most-recent reading and reports (latest_value, zscore,
-- trend, as_of, is_stale). `is_stale` flags a feed that has gone quiet past its
-- expected cadence — the "flatline" half of the EKG. Leads 7 (Parametric-
-- Trigger Proximity) and 10 (Macro→Loss Transmission) are view-sketch-only this
-- wave (see below / xdomain.sql) and are not yet arms of the panel.
--
-- Each arm is a latest-row pick via ROW_NUMBER(); trend is the sign of the
-- most recent change, staleness a per-lead cadence window. Pure view — promote
-- to a Lakeflow materialized table if the UNION fan-out gets slow.
CREATE OR REPLACE VIEW gold.market_ekg AS
WITH reins AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY observation_date DESC) rn
    FROM bronze.reinsurance_pricing
),
catld AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY observation_date DESC) rn
    FROM bronze.cat_load_nowcast
),
sev AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY observation_date DESC) rn
    FROM bronze.severity_index
),
lit AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY as_of DESC) rn
    FROM silver.litigation_pressure
),
disc AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY as_of DESC) rn
    FROM silver.disclosure_sentiment
),
resv AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY as_of DESC) rn
    FROM silver.reserving_signals
),
cap AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY as_of DESC) rn
    FROM silver.capital_flows
),
burden AS (
    SELECT state, day, burden_pressure,
           ROW_NUMBER() OVER (ORDER BY day DESC) rn
    FROM gold.burden_by_state
)
SELECT 1 AS lead, 'Reinsurance Pulse'            AS lead_name, 'market_cycle' AS hardens,
       value AS latest_value, zscore_12m AS zscore,
       CASE WHEN trend = 'firming' THEN 'up' WHEN trend = 'softening' THEN 'down' ELSE 'flat' END AS trend,
       CAST(observation_date AS TIMESTAMP) AS as_of,
       observation_date < CURRENT_DATE - INTERVAL 120 DAYS AS is_stale
FROM reins WHERE rn = 1
UNION ALL
SELECT 2, 'CAT-Load Nowcast', 'cat_load',
       value, zscore_12m,
       CASE WHEN mom_pct_change > 0 THEN 'up' WHEN mom_pct_change < 0 THEN 'down' ELSE 'flat' END,
       CAST(observation_date AS TIMESTAMP),
       observation_date < CURRENT_DATE - INTERVAL 14 DAYS
FROM catld WHERE rn = 1
UNION ALL
SELECT 3, 'Severity Tape', 'inflation_keyword_boost',
       value, zscore_12m,
       CASE WHEN mom_pct_change > 0 THEN 'up' WHEN mom_pct_change < 0 THEN 'down' ELSE 'flat' END,
       CAST(observation_date AS TIMESTAMP),
       observation_date < CURRENT_DATE - INTERVAL 45 DAYS
FROM sev WHERE rn = 1
UNION ALL
SELECT 4, 'Litigation Pressure', 'litigation_tplf_boost',
       pressure_index, NULL,
       NULL, as_of,
       as_of < CURRENT_TIMESTAMP - INTERVAL 30 DAYS
FROM lit WHERE rn = 1
UNION ALL
SELECT 5, 'Disclosure Sentiment', 'reserve_deterioration_boost',
       adverse_language_score, NULL,
       reserve_tone, as_of,
       as_of < CURRENT_TIMESTAMP - INTERVAL 100 DAYS
FROM disc WHERE rn = 1
UNION ALL
SELECT 6, 'Reserve-Adequacy Radar', 'reserve_deterioration_boost',
       deterioration_pct, NULL,
       direction, as_of,
       as_of < CURRENT_TIMESTAMP - INTERVAL 100 DAYS
FROM resv WHERE rn = 1
UNION ALL
SELECT 8, 'InsurTech Capital-Flow', 'ai_insurtech',
       amount_usd, NULL,
       deal_type, as_of,
       as_of < CURRENT_TIMESTAMP - INTERVAL 30 DAYS
FROM cap WHERE rn = 1
UNION ALL
SELECT 9, 'Regulatory Burden Barometer', 'burden_intensity_boost',
       burden_pressure, NULL,
       NULL, CAST(day AS TIMESTAMP),
       day < CURRENT_DATE - INTERVAL 14 DAYS
FROM burden WHERE rn = 1;

-- ── Lead 7 — Parametric-Trigger Proximity (VIEW SKETCH ONLY) ──────────────
-- No physical table this wave. The reading lives in the metadata_json of the
-- existing NHC wind-probability / USGS ShakeMap items already in
-- bronze.ingested_items (source IN ('nhc','usgs')). A trigger-band field is
-- the distance between the live hazard reading and a parametric cat-bond /
-- ILW attachment point. Sketch — uncomment + harden once a trigger-band is
-- captured at ingest time, and upgrade the geospatial join to H3 / Databricks
-- Mosaic for radius-to-exposure proximity:
--
-- CREATE OR REPLACE VIEW gold.trigger_proximity AS
-- SELECT b.source, b.title, b.published_at,
--        get_json_object(b.metadata_json, '$.trigger_band')   AS trigger_band,
--        get_json_object(b.metadata_json, '$.peak_value')     AS peak_value,    -- max wind prob / PGA
--        get_json_object(b.metadata_json, '$.attachment')     AS attachment,    -- bond/ILW attachment point
--        get_json_object(b.metadata_json, '$.h3_cell')        AS h3_cell        -- H3 index for exposure join
-- FROM bronze.ingested_items b
-- WHERE b.source IN ('nhc','usgs')
--   AND get_json_object(b.metadata_json, '$.trigger_band') IS NOT NULL;

-- Lead 9: add state to a pre-existing triage_verdicts (no-op if already present)
ALTER TABLE silver.triage_verdicts ADD COLUMN state STRING;