-- Gold layer — curated views for read consumption. These are views, not
-- materialized tables, so they always reflect current bronze/silver state.
-- Promote any view to a materialized table (Delta Live Tables) if query
-- latency becomes a concern.
--
-- Apply after bronze.sql + silver.sql.

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

-- Score calibration — system score vs the user's manual rating from Obsidian
-- _meta/Score Higher.md (Wave 4: populated by future scanning job). Empty
-- until silver.manual_ratings has data; gracefully no-output before then.
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
