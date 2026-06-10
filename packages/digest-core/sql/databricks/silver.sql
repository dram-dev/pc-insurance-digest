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
    state             STRING,                    -- Wave 4 Lead 9: US state code for regulatory_rate items (null elsewhere)
    model_id          STRING,                    -- triage model identifier
    CONSTRAINT pc_silver_triage_pk PRIMARY KEY (item_hash, triaged_at)
)
USING DELTA;
-- Lead 9 migration on an existing table:
--   ALTER TABLE pc_silver.triage_verdicts ADD COLUMN state STRING;

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
    reserve_boost     DOUBLE,                       -- Option 5: adverse reserve development on a named insurer
    learned_score     DOUBLE,                       -- Option 4: learned relevance, alongside the heuristic
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
    stock_move_z    DOUBLE,                       -- signed σ of the insurer's RAW return (own vol)
    stock_move_band STRING,                       -- 0.5/0.75/1.0/1.25/1.5/1.75/2.0/2+
    stock_move_excess_z DOUBLE,                   -- σ of the IAK/SPY-EXCESS return (idiosyncratic)
    stock_move_p    DOUBLE,                       -- two-sided p of the gating z (BH-FDR input)
    CONSTRAINT pc_silver_backtest_pk PRIMARY KEY (item_hash, horizon_days)
)
USING DELTA;
-- Existing deployments (table created before the excess-z columns):
--   ALTER TABLE pc_silver.outcome_backtest
--     ADD COLUMNS (stock_move_excess_z DOUBLE, stock_move_p DOUBLE);

-- Learned relevance score (Option 4): the numpy logistic-regression model's
-- predicted P(corroborated), written alongside the heuristic so gold can A/B
-- them. model_id references the SQLite learned_models registry.
CREATE TABLE IF NOT EXISTS pc_silver.learned_scores (
    item_hash     STRING    NOT NULL,
    model_id      INT       NOT NULL,
    learned_score DOUBLE    NOT NULL,
    scored_at     TIMESTAMP NOT NULL,
    CONSTRAINT pc_silver_learned_pk PRIMARY KEY (item_hash, model_id)
)
USING DELTA;

-- Reserving estimates (Option 5): chain-ladder ultimate / IBNR per insurer /
-- LOB / metric, with deterioration vs. the prior estimate. Insurer-keyed (a
-- derived actuarial fact, not a news item).
CREATE TABLE IF NOT EXISTS pc_silver.reserving_signals (
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
    CONSTRAINT pc_silver_reserving_pk PRIMARY KEY (insurer, lob, metric, as_of)
)
USING DELTA;

-- Frequency x severity x pure premium per accident year (digest.freq_sev),
-- derived from the XBRL facts: 'product' grain carries severity (incurred /
-- reported claims); 'segment' grain adds the earned-premium denominator
-- (frequency = claims per $M EP -- EP is the exposure PROXY -- and pure
-- premium = incurred/EP). Insurer/LOB-keyed derived facts, like reserving.
CREATE TABLE IF NOT EXISTS pc_silver.freq_sev_signals (
    insurer             STRING NOT NULL,
    grain               STRING NOT NULL,          -- 'product' | 'segment'
    lob                 STRING NOT NULL,
    accident_year       INT    NOT NULL,
    reported_claims     DOUBLE,
    incurred_musd       DOUBLE,
    earned_premium_musd DOUBLE,                   -- segment grain only
    severity_usd        DOUBLE,
    frequency_per_musd  DOUBLE,
    pure_premium_ratio  DOUBLE,
    as_of               STRING NOT NULL,
    CONSTRAINT pc_silver_freq_sev_pk PRIMARY KEY (insurer, grain, lob, accident_year, as_of)
)
USING DELTA;

-- Component-level insurer XBRL facts (concept registry — datasets 1-13). One row
-- per (concept × dimensional context) from the top-10 SEC-filing P&C insurers'
-- 10-K instances. Mirrors the SQLite insurer_xbrl_facts table.
CREATE TABLE IF NOT EXISTS pc_silver.insurer_xbrl_facts (
    fact_key         STRING NOT NULL,
    insurer          STRING NOT NULL,
    dataset          STRING NOT NULL,            -- premiums|claim_counts|ibnr|triangle|…
    concept          STRING NOT NULL,            -- us-gaap localname
    field            STRING,
    period_end       STRING,
    period_type      STRING,                     -- 'instant' | 'duration'
    accident_year    INT,
    segment          STRING,
    product          STRING,
    subsegment       STRING,
    geography        STRING,
    investment_type  STRING,
    instrument       STRING,
    fv_level         STRING,
    value            DOUBLE NOT NULL,            -- USD millions (or raw count)
    is_count         INT,
    as_of            STRING,
    CONSTRAINT pc_silver_xbrl_facts_pk PRIMARY KEY (fact_key)
)
USING DELTA;

-- Statutory high-level facts for insurers NOT in SEC XBRL (the big mutuals) —
-- NAIC InsData Schedule P summary + free III top-writer tables. Mirrors the
-- SQLite statutory_facts table.
CREATE TABLE IF NOT EXISTS pc_silver.statutory_facts (
    fact_key      STRING NOT NULL,
    insurer       STRING NOT NULL,
    source        STRING NOT NULL,               -- 'naic_insdata'|'iii'|'annual_report'
    dataset       STRING NOT NULL,               -- premiums|combined_ratio|surplus|market_share
    field         STRING,
    line          STRING,
    accident_year INT,
    period        STRING,
    value         DOUBLE NOT NULL,
    unit          STRING,
    as_of         STRING,
    canonical_lob STRING,                         -- unified taxonomy
    CONSTRAINT pc_silver_statutory_pk PRIMARY KEY (fact_key)
)
USING DELTA;

-- ── Wave 4 — Insurance EKG leads (derived facts) ─────────────────────────
-- See docs/WAVE4_EKG_PLAN.md. Ingestors are future waves; the DatabricksSink
-- already has a no-op write_* per table.

-- Lead 4 — Litigation Pressure Index. Marathon nuclear-verdict tracker +
-- Westfleet TPLF survey + CourtListener docket velocity, rolled to a
-- per-state × sector pressure index. Hardens signals.litigation_tplf_boost.
-- (state, sector)-keyed — a derived legal-environment fact, not a news item.
CREATE TABLE IF NOT EXISTS pc_silver.litigation_pressure (
    state            STRING    NOT NULL,           -- US state code, or 'US' for the national roll-up
    sector           STRING    NOT NULL,           -- 'commercial_auto' | 'product_liability' | 'med_mal' | …
    as_of            TIMESTAMP NOT NULL,
    verdict_count    INT,                           -- nuclear verdicts (≥$10M) in the window
    median_award     DOUBLE,                        -- USD
    tplf_commitments DOUBLE,                        -- disclosed third-party funding committed, USD
    docket_velocity  DOUBLE,                        -- new P&C dockets / day, trailing window
    pressure_index   DOUBLE,                        -- composite 0-100, drives the boost calibration
    CONSTRAINT pc_silver_litigation_pk PRIMARY KEY (state, sector, as_of)
)
USING DELTA;

-- Lead 5 — Disclosure Sentiment. Reserve-tone NLP (FinBERT / Loughran-McDonald,
-- or ai_query() on the warehouse) over EDGAR MD&A / reserve footnotes. Hardens
-- signals reserve_deterioration_boost with a *language* read that leads the
-- chain-ladder number. (insurer, period)-keyed.
CREATE TABLE IF NOT EXISTS pc_silver.disclosure_sentiment (
    insurer               STRING    NOT NULL,       -- ticker
    period                STRING    NOT NULL,       -- filing period, e.g. '2026Q1'
    as_of                 TIMESTAMP NOT NULL,        -- filing date
    reserve_tone          STRING,                    -- 'strengthening' | 'releasing' | 'neutral'
    adverse_language_score DOUBLE,                   -- 0.0-1.0, higher = more adverse framing
    source_filing         STRING,                    -- accession number or filing URL
    CONSTRAINT pc_silver_disclosure_pk PRIMARY KEY (insurer, period, as_of)
)
USING DELTA;

-- Lead 8 — InsurTech Capital-Flow. Structured extraction (ai_query() on the
-- warehouse / Ollama locally) of funding-round + broker-M&A news into deal
-- facts. Powers the ai_insurtech topic with substance so the 35% share cap
-- stops being the only governor. item_hash-keyed back to the source news item.
CREATE TABLE IF NOT EXISTS pc_silver.capital_flows (
    item_hash   STRING    NOT NULL,                 -- joins to bronze.ingested_items
    as_of       TIMESTAMP NOT NULL,
    deal_type   STRING,                              -- 'funding_round' | 'm&a' | 'broker_acquisition' | 'ipo'
    amount_usd  DOUBLE,
    stage       STRING,                              -- 'seed' | 'series_a' | … | null for M&A
    target      STRING,                              -- company acquired / funded
    investors   STRING,                              -- JSON array or comma-list of investors / acquirer
    CONSTRAINT pc_silver_capital_flows_pk PRIMARY KEY (item_hash)
)
USING DELTA;

-- Alpha engine — per-(insurer, as-of, horizon) forward-return forecasts from
-- the local returns model. Advisory only; never feeds the heuristic
-- leaderboard. Joins to bronze.prices for realized-vs-predicted accuracy.
CREATE TABLE IF NOT EXISTS pc_silver.return_forecasts (
    ticker       STRING NOT NULL,
    as_of        DATE   NOT NULL,                       -- prediction date
    horizon_days INT    NOT NULL,
    pred_excess  DOUBLE,                                 -- predicted excess return vs benchmark
    pred_prob    DOUBLE,                                 -- P(beats peer by ≥1σ), classifier head
    model_id     BIGINT,
    scored_at    TIMESTAMP,
    CONSTRAINT pc_silver_return_forecasts_pk PRIMARY KEY (ticker, as_of, horizon_days)
)
USING DELTA
PARTITIONED BY (horizon_days);
