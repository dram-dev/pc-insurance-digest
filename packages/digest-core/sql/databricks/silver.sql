-- Silver layer — cleansed and joined. Each row references a bronze item_hash;
-- separate tables for triage / score / summary so producers stay decoupled
-- (a re-score doesn't bump triage history, etc.).
--
-- Apply after bronze.sql.

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
    model_id          STRING,                    -- triage model identifier
    CONSTRAINT silver_triage_pk PRIMARY KEY (item_hash, triaged_at)
)
USING DELTA;

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

-- Manual ratings from Obsidian _meta/Score Higher.md (Wave 4 — will be
-- populated by a future scanning job). Empty until then; gold.score_calibration
-- left-joins this table so it gracefully degrades to no-output.
CREATE TABLE IF NOT EXISTS silver.manual_ratings (
    item_hash   STRING    NOT NULL,
    rated_at    TIMESTAMP NOT NULL,
    user_rating DOUBLE,                          -- 1.0-5.0, user's target score
    note        STRING,                          -- freeform context from the user
    CONSTRAINT silver_manual_pk PRIMARY KEY (item_hash, rated_at)
)
USING DELTA;
