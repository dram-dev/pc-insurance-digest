-- Silver layer — cleansed and joined. Each row references a bronze item_hash;
-- separate tables for triage / score / summary so producers stay decoupled
-- (a re-score doesn't bump triage history, etc.).
--
-- PC Digest's silver (`pc_silver`) in the shared `digest` catalog. Apply after
-- bronze (pc_bronze) DDL.

CREATE SCHEMA IF NOT EXISTS pc_silver;

-- Triage verdict per item per triage run. Drop_reason is the model's reason
-- string when decision='drop'; for keep items, `reason` holds the keep rationale.
CREATE TABLE IF NOT EXISTS pc_silver.triage_verdicts (
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
    model_id          STRING,                    -- triage model identifier
    CONSTRAINT pc_silver_triage_pk PRIMARY KEY (item_hash, triaged_at)
)
USING DELTA;

-- Per-item leaderboard score with all 11 multiplicative factors broken out.
-- Enables back-testing: "if I bump PGR insurer_boost to 1.7×, what would last
-- week's top-5 have looked like?" Columns mirror SQLite signal_scores exactly.
CREATE TABLE IF NOT EXISTS pc_silver.signal_scores (
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
    CONSTRAINT pc_silver_score_pk PRIMARY KEY (item_hash, computed_at)
)
USING DELTA;

-- Summarizer output + materiality. Stub-summarized items (EDGAR / FRED short-
-- circuits in summarize.py) also land here, with model_id set to 'stub'.
CREATE TABLE IF NOT EXISTS pc_silver.summaries (
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
    CONSTRAINT pc_silver_summaries_pk PRIMARY KEY (item_hash, summarized_at)
)
USING DELTA;

-- Manual ratings from `digest rate` / Obsidian _meta/Score Higher.md. Populated
-- by the calibration loop (Databricks Option 1a); gold.score_calibration
-- left-joins this so it gracefully degrades to no-output when empty.
CREATE TABLE IF NOT EXISTS pc_silver.manual_ratings (
    item_hash   STRING    NOT NULL,
    rated_at    TIMESTAMP NOT NULL,
    user_rating DOUBLE,                          -- 1.0-5.0, user's target score
    note        STRING,                          -- freeform context from the user
    CONSTRAINT pc_silver_manual_pk PRIMARY KEY (item_hash, rated_at)
)
USING DELTA;

-- Outcome backtest (Option 1b): did a ranked item actually matter, N days on?
-- One row per (item, horizon ∈ {7,30}); corroborated = any of 5 signals fired.
-- Feeds gold.outcome_hit_rate + the Option-4 learned scorer's training labels.
CREATE TABLE IF NOT EXISTS pc_silver.outcome_backtest (
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
    CONSTRAINT pc_silver_backtest_pk PRIMARY KEY (item_hash, horizon_days)
)
USING DELTA;
