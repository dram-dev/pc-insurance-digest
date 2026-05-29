-- Gold layer — curated views for read consumption. These are views, not
-- materialized tables, so they always reflect current bronze/silver state.
-- Promote any view to a materialized table (Lakeflow/DLT) if query latency
-- becomes a concern.
--
-- PC Digest's gold (`pc_gold`) in the shared `digest` catalog. Apply after
-- pc_bronze + pc_silver DDL.

CREATE SCHEMA IF NOT EXISTS pc_gold;

-- Latest-per-item score (one row per item_hash, most recent computed_at).
-- Most gold views need this; materialize it via a small helper view.
CREATE OR REPLACE VIEW pc_gold.latest_scores AS
SELECT *
FROM (
    SELECT
        s.*,
        ROW_NUMBER() OVER (PARTITION BY item_hash ORDER BY computed_at DESC) AS rn
    FROM pc_silver.signal_scores s
)
WHERE rn = 1;

-- Daily leaderboard — top items per day with full score breakdown.
CREATE OR REPLACE VIEW pc_gold.daily_leaderboard AS
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
FROM pc_gold.latest_scores s
JOIN pc_silver.triage_verdicts t  USING (item_hash)
JOIN pc_bronze.ingested_items b   USING (item_hash)
LEFT JOIN pc_silver.summaries sm  USING (item_hash)
WHERE t.decision = 'keep';

-- Weekly leaderboard — same shape, weekly partition.
CREATE OR REPLACE VIEW pc_gold.weekly_leaderboard AS
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
FROM pc_gold.latest_scores s
JOIN pc_silver.triage_verdicts t  USING (item_hash)
JOIN pc_bronze.ingested_items b   USING (item_hash)
LEFT JOIN pc_silver.summaries sm  USING (item_hash)
WHERE t.decision = 'keep';

-- Per-source quality — keep rate, top drop reasons, avg materiality.
-- Surfaces noisy sources and validates the triage tightening from Wave 3 Phase 1.
CREATE OR REPLACE VIEW pc_gold.source_quality AS
SELECT
    b.source,
    DATE(b.ingested_at)                                            AS day,
    COUNT(*)                                                        AS items_ingested,
    COUNT_IF(t.decision = 'keep')                                   AS items_kept,
    COUNT_IF(t.decision = 'drop')                                   AS items_dropped,
    ROUND(COUNT_IF(t.decision = 'keep') / NULLIF(COUNT(*), 0), 3)   AS keep_rate,
    AVG(CASE WHEN t.decision = 'keep' THEN sm.materiality END)      AS avg_materiality,
    AVG(CASE WHEN t.decision = 'keep' THEN s.score END)             AS avg_score
FROM pc_bronze.ingested_items b
LEFT JOIN pc_silver.triage_verdicts t  USING (item_hash)
LEFT JOIN pc_silver.summaries sm       USING (item_hash)
LEFT JOIN pc_gold.latest_scores s      USING (item_hash)
GROUP BY b.source, DATE(b.ingested_at);

-- Score calibration — system score vs the user's manual rating (digest rate /
-- Obsidian _meta). Live once pc_silver.manual_ratings has data; gracefully
-- no-output before then.
CREATE OR REPLACE VIEW pc_gold.score_calibration AS
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
FROM pc_silver.manual_ratings m
LEFT JOIN pc_gold.latest_scores s   USING (item_hash)
LEFT JOIN pc_silver.triage_verdicts t USING (item_hash)
LEFT JOIN pc_bronze.ingested_items b  USING (item_hash);

-- Regime history with prevailing regime at any point in time.
CREATE OR REPLACE VIEW pc_gold.regime_history AS
SELECT
    as_of,
    market_cycle,
    cat_load,
    market_cycle_mult,
    cat_load_mult,
    multiplier,
    source,
    evidence_json
FROM pc_bronze.regime_signals
ORDER BY as_of DESC;

-- Operational SLOs by source + stage. Watch this when a feed silently degrades.
CREATE OR REPLACE VIEW pc_gold.pipeline_slos AS
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
FROM pc_bronze.pipeline_telemetry
GROUP BY stage, source, DATE(started_at);

-- ── Option 2 analytics views (Genie-friendly) ───────────────────────────

-- Topic volume over time — kept items per topic per day. Powers "which topics
-- are heating up?" in Genie + a topic-trend dashboard tile.
CREATE OR REPLACE VIEW pc_gold.topic_trend AS
SELECT
    DATE(t.triaged_at)                                             AS day,
    t.topic,
    COUNT(*)                                                        AS items_kept,
    AVG(sm.materiality)                                            AS avg_materiality,
    AVG(s.score)                                                   AS avg_score
FROM pc_silver.triage_verdicts t
LEFT JOIN pc_silver.summaries sm   USING (item_hash)
LEFT JOIN pc_gold.latest_scores s  USING (item_hash)
WHERE t.decision = 'keep'
GROUP BY DATE(t.triaged_at), t.topic;

-- Regulatory burden trend — counts by intensity/direction over time, the
-- Regulatory Sonar surface in the lakehouse. Drives the burden Alert + a
-- "burden pressure" dashboard tile. (Per-state breakdown awaits a state field
-- captured at triage time; today this is intensity × direction.)
CREATE OR REPLACE VIEW pc_gold.burden_trend AS
SELECT
    DATE(t.triaged_at)                                             AS day,
    t.burden_intensity,
    t.burden_direction,
    COUNT(*)                                                        AS items
FROM pc_silver.triage_verdicts t
WHERE t.decision = 'keep'
  AND t.topic = 'regulatory_rate'
  AND t.burden_intensity IS NOT NULL
GROUP BY DATE(t.triaged_at), t.burden_intensity, t.burden_direction;

-- ── Option 1b: outcome calibration ───────────────────────────────────────

-- Top-N precision — of items ranked in the daily top-N, what fraction
-- corroborated at each horizon? The headline "is the leaderboard right?" metric.
CREATE OR REPLACE VIEW pc_gold.outcome_hit_rate AS
WITH ranked AS (
    SELECT DATE(s.computed_at) AS day, s.item_hash,
           ROW_NUMBER() OVER (PARTITION BY DATE(s.computed_at)
                              ORDER BY s.score DESC) AS rnk
    FROM pc_gold.latest_scores s
    JOIN pc_silver.triage_verdicts t USING (item_hash)
    WHERE t.decision = 'keep'
)
SELECT r.day, o.horizon_days, COUNT(*) AS ranked_items,
       SUM(CASE WHEN o.corroborated THEN 1 ELSE 0 END) AS corroborated,
       ROUND(AVG(CASE WHEN o.corroborated THEN 1.0 ELSE 0.0 END), 3) AS hit_rate
FROM ranked r
JOIN pc_silver.outcome_backtest o USING (item_hash)
WHERE r.rnk <= 5
GROUP BY r.day, o.horizon_days;

-- Corroboration rate by topic / source / tier — which slices the leaderboard
-- gets right vs. wrong. Directly drives scoring-weight tuning + is the Option-4
-- training signal. (Tier from latest_scores.)
CREATE OR REPLACE VIEW pc_gold.outcome_by_factor AS
SELECT
    o.horizon_days,
    t.topic,
    b.source,
    s.tier,
    COUNT(*)                                                       AS n,
    ROUND(AVG(CASE WHEN o.corroborated THEN 1.0 ELSE 0.0 END), 3)  AS corroboration_rate
FROM pc_silver.outcome_backtest o
JOIN pc_silver.triage_verdicts t  USING (item_hash)
JOIN pc_bronze.ingested_items b   USING (item_hash)
LEFT JOIN pc_gold.latest_scores s USING (item_hash)
GROUP BY o.horizon_days, t.topic, b.source, s.tier;

-- ── Option 5: reserving ───────────────────────────────────────────────────

-- Latest chain-ladder estimate per insurer/LOB/metric, ranked by how adverse
-- the reserve development is. Feeds the reserving digest callout + (once wired)
-- the reserve_deterioration_boost.
CREATE OR REPLACE VIEW pc_gold.reserving_signals AS
WITH latest AS (
    SELECT insurer, lob, metric, MAX(as_of) AS as_of
    FROM pc_silver.reserving_signals GROUP BY insurer, lob, metric
)
SELECT r.insurer, r.lob, r.metric, r.as_of,
       r.ultimate, r.latest, r.ibnr, r.prior_ibnr,
       r.deterioration_pct, r.direction
FROM pc_silver.reserving_signals r
JOIN latest l ON r.insurer = l.insurer AND r.lob = l.lob
             AND r.metric = l.metric AND r.as_of = l.as_of
ORDER BY ABS(COALESCE(r.deterioration_pct, 0)) DESC;
