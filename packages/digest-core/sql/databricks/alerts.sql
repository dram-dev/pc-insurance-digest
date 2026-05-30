-- Databricks SQL Alerts (Option 2) — each query RETURNS ROWS ONLY WHEN ITS
-- CONDITION FIRES, so wire it to an Alert with "trigger when result is not
-- empty" (row count > 0) and a notification destination. These are PC's
-- (pc_* schemas); macro can mirror with macro_*. The local equivalent is
-- `digest brief`.
--
-- Save each SELECT as its own Databricks SQL query, then create an Alert on it.

-- 1) REGIME FLIP — the prevailing market_cycle or cat_load just changed.
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY as_of DESC) AS rn
    FROM pc_bronze.regime_signals
)
SELECT a.as_of, a.market_cycle, a.cat_load, a.multiplier,
       b.market_cycle AS prev_market_cycle, b.cat_load AS prev_cat_load
FROM ranked a
JOIN ranked b ON b.rn = 2
WHERE a.rn = 1
  AND (a.market_cycle <> b.market_cycle OR a.cat_load <> b.cat_load);

-- 2) HIGH REGULATORY BURDEN — new high-intensity regulatory items (last 24h).
SELECT b.title, b.source, t.burden_direction, t.triaged_at
FROM pc_silver.triage_verdicts t
JOIN pc_bronze.ingested_items b USING (item_hash)
WHERE t.decision = 'keep'
  AND t.topic = 'regulatory_rate'
  AND t.burden_intensity = 'high'
  AND t.triaged_at >= CURRENT_TIMESTAMP - INTERVAL 24 HOURS;

-- 3) LITIGATION / NUCLEAR VERDICT — TPLF / mass-tort signals (last 24h).
SELECT b.title, b.source, t.triaged_at
FROM pc_silver.triage_verdicts t
JOIN pc_bronze.ingested_items b USING (item_hash)
WHERE t.decision = 'keep'
  AND ARRAY_CONTAINS(t.sub_tags, 'litigation_tplf')
  AND t.triaged_at >= CURRENT_TIMESTAMP - INTERVAL 24 HOURS;

-- 4) SOURCE DEGRADATION — a source's keep-rate today fell well below its
-- trailing-14d average (possible feed change / quality drop), or it errored.
WITH recent AS (
    SELECT source, keep_rate, day FROM pc_gold.source_quality
    WHERE day = CURRENT_DATE
),
baseline AS (
    SELECT source, AVG(keep_rate) AS avg_keep_rate
    FROM pc_gold.source_quality
    WHERE day >= CURRENT_DATE - INTERVAL 14 DAYS AND day < CURRENT_DATE
    GROUP BY source
)
SELECT r.source, r.keep_rate AS today_keep_rate, b.avg_keep_rate
FROM recent r
JOIN baseline b USING (source)
WHERE b.avg_keep_rate > 0
  AND r.keep_rate < 0.5 * b.avg_keep_rate;

-- 5) FRED COST-DRIVER ANOMALY — a tracked series breached its ±σ gate (last 7d).
SELECT series_id, observation_date, value, zscore_12m
FROM pc_bronze.fred_observations
WHERE is_anomaly
  AND observation_date >= CURRENT_DATE - INTERVAL 7 DAYS;

-- ── Wave 4 — Insurance EKG panel alerts ───────────────────────────────────
-- One Alert per lead, read off pc_gold.market_ekg. Each fires when its lead is
-- either FLATLINING (feed stale past its cadence → is_stale) or SPIKING
-- (|zscore| past threshold; leads without a z fall back to is_stale only).
-- Save each SELECT as its own Databricks SQL query + Alert (trigger when
-- row count > 0). Spike threshold 2.5σ is a starting point — tune per lead once
-- live distributions exist.

-- EKG 1) Reinsurance Pulse — renewal pricing flatline or spike.
SELECT lead_name, latest_value, zscore, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 1 AND (is_stale OR ABS(COALESCE(zscore, 0)) >= 2.5);

-- EKG 2) CAT-Load Nowcast — hazard-load flatline or spike.
SELECT lead_name, latest_value, zscore, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 2 AND (is_stale OR ABS(COALESCE(zscore, 0)) >= 2.5);

-- EKG 3) Severity Tape — loss-cost inflation flatline or spike.
SELECT lead_name, latest_value, zscore, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 3 AND (is_stale OR ABS(COALESCE(zscore, 0)) >= 2.5);

-- EKG 4) Litigation Pressure — verdict/TPLF pressure surge or stale tracker.
SELECT lead_name, latest_value, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 4 AND (is_stale OR latest_value >= 70);   -- pressure_index 0-100

-- EKG 5) Disclosure Sentiment — adverse reserve-tone surge or stale.
SELECT lead_name, latest_value, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 5 AND (is_stale OR latest_value >= 0.6);  -- adverse_language_score 0-1

-- EKG 6) Reserve-Adequacy Radar — adverse chain-ladder development or stale.
SELECT lead_name, latest_value, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 6 AND (is_stale OR (trend = 'adverse' AND latest_value >= 0.10));

-- EKG 8) InsurTech Capital-Flow — large round/deal or stale feed.
SELECT lead_name, latest_value, trend, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 8 AND (is_stale OR latest_value >= 1e8);  -- $100M+ deal

-- EKG 9) Regulatory Burden Barometer — burden-pressure surge or stale.
SELECT lead_name, latest_value, as_of, is_stale
FROM pc_gold.market_ekg
WHERE lead = 9 AND (is_stale OR latest_value >= 9);    -- intensity-weighted pressure

-- EKG 7) Parametric-Trigger Proximity — VIEW SKETCH ONLY (see gold.sql); no
--        Alert until pc_gold.trigger_proximity is materialized.
-- EKG 10) Macro→Loss Transmission — VIEW SKETCH ONLY (see xdomain.sql); cross-
--        domain Alert deferred until macro_* lands in the shared catalog.
