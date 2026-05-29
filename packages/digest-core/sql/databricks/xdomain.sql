-- Cross-domain views — the payoff of the shared `digest` catalog: pc_* and
-- macro_* unioned so one Genie space / dashboard spans both digests.
--
-- Apply LAST, after pc_gold + macro_gold exist (run the macro repo's
-- sql/databricks/ DDL too). If only one domain is populated, the other side of
-- each UNION simply contributes no rows.

CREATE SCHEMA IF NOT EXISTS xdomain;

-- Source quality across both digests — keep rate + volume per source per day,
-- tagged with its domain. "Which domain's feeds are noisiest this week?"
CREATE OR REPLACE VIEW xdomain.source_quality AS
SELECT 'pc'    AS domain, source, day, items_ingested, items_kept,
       items_dropped, keep_rate, avg_materiality
FROM pc_gold.source_quality
UNION ALL
SELECT 'macro' AS domain, source, day, items_ingested, items_kept,
       items_dropped, keep_rate, avg_materiality
FROM macro_gold.source_quality;

-- Operational SLOs across both digests — one pane for pipeline health.
CREATE OR REPLACE VIEW xdomain.pipeline_slos AS
SELECT 'pc'    AS domain, stage, source, day, runs, items_in, items_out,
       avg_duration_ms, p95_duration_ms, total_errors
FROM pc_gold.pipeline_slos
UNION ALL
SELECT 'macro' AS domain, stage, source, day, runs, items_in, items_out,
       avg_duration_ms, p95_duration_ms, total_errors
FROM macro_gold.pipeline_slos;

-- macro_linkage (the headline cross-domain signal) — STUB / next iteration.
-- The goal: correlate macro's cost-driver signals (FRED CPI/PPI, rates) with
-- PC's loss-cost / regulatory signals, so a macro inflation print surfaces
-- alongside the P&C topics it should move (supply_chain, personal_lines,
-- reserving). Needs macro's FRED observations + topic volume in the lakehouse
-- (macro currently sinks the domain-agnostic subset only). Sketch once macro's
-- fred_observations / topic_trend land:
--   SELECT f.observation_date, f.series_id, f.zscore_12m,
--          p.day, p.topic, p.items_kept
--   FROM macro_bronze.fred_observations f
--   JOIN pc_gold.topic_trend p
--     ON p.day BETWEEN f.observation_date AND f.observation_date + INTERVAL 30 DAYS
--   WHERE f.is_anomaly AND p.topic IN ('supply_chain','personal_lines','reserving');
