# Wave 4 — The Insurance EKG

> A panel of ten vital-sign indicators ("leads") that harden PC Digest's
> existing **two-axis regime detector** (`market_cycle × cat_load`) and
> **12-factor signal leaderboard**. Each lead reads a real-world feed,
> reduces it to a value + z-score + trend, and wires that reading into a
> specific scoring symbol — so the digest's ranking moves when the P&C
> market's vitals move.

## Why "EKG"

The digest already *describes* the market (it triages and summarizes news).
The EKG makes it *monitor* the market: a small set of leads, each a
continuously-updated vital sign, read together as a panel. A clinician
doesn't read one lead — they read the 12-lead trace as a gestalt. Same
here: `pc_gold.market_ekg` is the panel view (one row per lead: latest
value, z-score, trend, staleness flag), `pc_gold` Alerts are the monitor
(flatline = a feed went quiet; spike = |z| breached threshold), and a
Genie space is the bedside conversation ("which leads are abnormal this
week, and what did they move?").

Crucially, **no lead is a new top-level output**. Every lead terminates in
an existing wiring target — a `regime` multiplier or a `signals` boost —
so the EKG sharpens the machine we already have rather than bolting on a
parallel one.

## Sequencing

Ship in leverage order, not lead-number order:

1. **Lead 6 — Reserve-Adequacy Radar** *(highest leverage; prototyped this
   wave)*. The entire downstream is already wired (commit `365f4b7`):
   `loss_triangles → reserving.run_reserving() → reserving_signals →
   db.reserving_severity_map() → signals._reserve_deterioration_boost() →
   leaderboard`. The only missing link was *PDF table → triangle cells*,
   delivered here as `parse.triangles`.
2. **Lead 5 — Disclosure Sentiment**. Reuses the same EDGAR fetch surface
   as Lead 6 and feeds the *same* boost (`reserve_deterioration_boost`)
   with a language read that *leads* the chain-ladder number.
3. **Leads 1 + 2 — Reinsurance Pulse & CAT-Load Nowcast**. These harden
   the two regime axes directly — the highest-fan-out change, since the
   regime multiplier touches every item's score.
4. **The rest** (3, 4, 8, 9), then the two sketch-only leads (7, 10) when
   their heavier primitives (H3/Mosaic geospatial; cross-domain Delta
   Sharing) are worth the lift.

## Free-Edition reality note

Databricks Free Edition is CPU-only. The following are available and are
the assumed warehouse primitives: **`ai_query()`** (LLM calls in SQL),
**`ai_forecast()`** (time-series projection), **Genie** (NL-to-SQL),
**Databricks SQL Alerts**, and **Lakeflow DLT** (declarative pipelines).
The following are preview / GPU-heavy / not dependable on Free Edition:
**`ai_parse_document()`** (structured PDF/OCR extraction) and **Vector
Search** (managed embeddings index). Therefore **every lead has a
local-first path** that runs on the Mac mini today, with the Databricks
primitive documented as the upgrade:

| Warehouse primitive | Local-first fallback |
|---|---|
| `ai_query()` (LLM in SQL) | MLX Qwen3.5 server (the summarizer backend) |
| `ai_parse_document()` (PDF tables/OCR) | `pdfplumber` + `parse.pdf_tables` + `parse.triangles` |
| Vector Search (managed index) | Option-3 Ollama embeddings in `pc_bronze.item_embeddings` (JSON vectors) |
| `ai_forecast()` | numpy trend/z-score on the trailing window (as in `fred.py`) |

SQLite remains the source of truth; the medallion sink is best-effort and
no-op unless `DATABRICKS_ENABLED=true`.

---

## The 10 leads

Template per lead: **Vital sign** (which axis/boost it hardens) ·
**Source & access** · **Databricks primitive + Free-Edition local
fallback** · **Wiring target** (exact module + symbol) · **Status**.

### Lead 1 — Reinsurance Pulse

- **Vital sign.** Hardens `regime.market_cycle` (hard ↔ soft cycle
  position). Rate-on-line and ILS spreads are the cleanest market-priced
  read on where the reinsurance cycle sits.
- **Source & access.** Guy Carpenter Global Property Cat Rate-on-Line
  Index (published commentary / annual + renewal updates; scrape/manual).
  Artemis Deal Directory + Lane Financial ILS/cat-bond spread series
  (Artemis: free web; Lane: published quarterly reviews). All US/global
  property-cat + retro segments.
- **Primitive + fallback.** `ai_forecast()` to project next renewal's
  direction off the ROL/spread series → local: numpy slope + z-score over
  the trailing renewals, same shape as `fred.py`.
- **Wiring target.** `regime` market-cycle inference — feed the index
  level + trend into the `market_cycle` classifier (today narrative-only)
  so `RegimeSignal.market_cycle_mult` reflects priced ROL, not just
  trade-press tone. Table: `pc_bronze.reinsurance_pricing`.
- **Status.** **Shipped** — `reinsurance.reduce_series`/`market_cycle_hint` +
  `regime._apply_pricing_hint` (firm-only, neutral until data). Fetchers are a
  config scaffold (`reinsurance_sources.yaml`, all `enabled:false`) pending
  Mac-mini validation; reducer + regime hook tested.

### Lead 2 — CAT-Load Nowcast

- **Vital sign.** Hardens `regime.cat_load`
  (`low_season`/`active_season`/`post_major_event`). Turns cat-load from a
  manual/threshold call into a live nowcast.
- **Source & access.** OpenFEMA Disaster Declarations API (free, no key) ·
  NOAA CPC seasonal outlooks (free) · US Drought Monitor (free API/CSV) ·
  PowerOutage.us (scrape / paid tiers; free national snapshot). Region-
  scoped (per-state + US roll-up).
- **Primitive + fallback.** Lakeflow DLT to maintain the rolling nowcast +
  `ai_forecast()` on declaration velocity → local: scheduled ingestor
  computing z-scores vs. the trailing-12m baseline.
- **Wiring target.** `regime.cat_load` classifier + its `cat_load_mult`.
  A spike in open declarations / outage customers → `post_major_event`
  (1.20×); elevated CPC outlook → `active_season` (1.10×). Table:
  `pc_bronze.cat_load_nowcast`.
- **Status.** **Shipped + live-validated** — `cat_nowcast` pulls monthly OpenFEMA
  disaster counts (free, no key) → 12m z-score → escalate-only nudge in
  `regime.compute_cat_load` (`digest cat-nowcast`). Validated on the Mac mini.

### Lead 3 — Severity Tape

- **Vital sign.** Hardens the loss-cost inflation boosts. A forward read on
  claim severity (parts, labor, used-vehicle values, medical).
- **Source & access.** Manheim Used Vehicle Value Index (published
  monthly; scrape/manual) + the **existing** FRED parts/labor/medical
  series already ingested by `fred.py`. Unified into one tape.
- **Primitive + fallback.** `ai_forecast()` + Feature Store on the index
  levels → local: extend `fred.py`'s z-score gate to the Manheim series;
  reuse `auto_keep_quantitative()`.
- **Wiring target.** `signals._inflation_keyword_boost` calibration — the
  boost is a keyword hit today (1.2× flat); the tape lets it scale with
  the *magnitude* of the severity regime (e.g. lift to 1.3× when the tape
  is z>2). Symbol: `signals._INFLATION_RE` hit → magnitude-scaled via the
  tape. Table: `pc_bronze.severity_index`.
- **Status.** **Shipped + live-validated** — `severity_tape` blends the existing
  FRED loss-cost series into one z-score; `signals._inflation_keyword_boost`
  uplifts (capped) when the tape is hot. `digest severity-tape`; validated on the
  Mac mini (Manheim UVVI is the documented additional component).

### Lead 4 — Litigation Pressure Index

- **Vital sign.** Hardens `signals.litigation_tplf_boost` (currently 1.3×
  flat on a keyword/sub_tag hit) with a per-state × sector pressure read.
- **Source & access.** Marathon Strategies nuclear-verdict tracker
  (published reports) · Westfleet Advisors TPLF investor survey (annual) ·
  **existing** CourtListener docket velocity (`ingest/courtlistener.py`).
- **Primitive + fallback.** Vector Search over verdict/docket text +
  `ai_query()` to extract award amounts → local: Ollama embeddings
  (Option 3) + MLX extraction; numpy pressure index.
- **Wiring target.** `signals.LITIGATION_TPLF_BOOST` → make it a function
  of `pc_silver.litigation_pressure.pressure_index` for the item's state/
  sector instead of a constant. Table: `pc_silver.litigation_pressure`.
- **Status.** **Shipped** — `litigation.compute_pressure_index` (verdict/award/
  TPLF/docket composite) + `_litigation_tplf_boost` pressure scaling. v1 computes
  the live CourtListener docket-velocity component (`digest litigation`); Marathon
  / Westfleet verdict+TPLF components pending scraper validation, so the index
  stays conservative until then. Reducer + boost scaling tested.

### Lead 5 — Disclosure Sentiment

- **Vital sign.** Hardens `signals reserve_deterioration_boost` with a
  *language* signal that leads the chain-ladder number — reserve tone in
  MD&A / footnotes often softens before the triangle confirms.
- **Source & access.** EDGAR 10-K/10-Q/8-K filings (already fetched by
  `ingest/edgar.py`); run reserve-tone NLP (FinBERT or Loughran-McDonald
  finance lexicon) over the reserve-discussion sections.
- **Primitive + fallback.** `ai_query()` for tone classification → local:
  Loughran-McDonald lexicon scoring (pure Python, no model) or MLX
  classification.
- **Wiring target.** `signals._reserve_deterioration_boost` /
  `db.reserving_severity_map()` — blend the language `adverse_language_
  score` into the severity map so an insurer with adverse *tone* gets a
  boost ahead of confirmed adverse *development*. Table:
  `pc_silver.disclosure_sentiment`.
- **Status.** Researched. DDL + sink scaffold shipped; ingestor pending.

### Lead 6 — Reserve-Adequacy Radar  **[HIGHEST LEVERAGE — prototyped]**

- **Vital sign.** Hardens `signals reserve_deterioration_boost` (Option 5)
  with real chain-ladder IBNR deterioration per insurer/LOB.
- **Source & access.** Loss-development triangles in insurer investor-
  supplement PDFs (`ingest/investor_supp.py`) and NAIC Schedule P
  (`ingest/naic_schedp.py`). Free; the access cost is *parsing* the PDF
  tables.
- **Primitive + fallback.** `ai_parse_document()` returns structured
  tables (incl. scanned-PDF OCR) directly → **local: `parse.triangles`
  (this wave)** — pure-stdlib orientation detection + accounting-number
  parsing over `parse.pdf_tables.Table`, into `db.upsert_triangle_cells()`.
- **Wiring target.** Already fully wired (commit `365f4b7`):
  `loss_triangles → reserving.run_reserving() → reserving_signals →
  db.reserving_severity_map() → signals._reserve_deterioration_boost() →
  score_item`. Activates automatically once the severity map is non-empty.
  Tables: `pc_bronze.loss_triangles`, `pc_silver.reserving_signals`.
- **Status.** **Prototyped this wave** — `parse.triangles.parse_triangle()`
  + `investor_supp` routing + end-to-end tests (synthetic Table → cells →
  chain-ladder → boost>1.0). Remaining: validate CSS/PDF templates on the
  Mac mini, flip `investor_supplements.yaml` insurers to `enabled:true`.

### Lead 7 — Parametric-Trigger Proximity  **[VIEW SKETCH ONLY]**

- **Vital sign.** Hardens `cat_event` + drives a dedicated Alert. How close
  a live hazard is to a parametric cat-bond / ILW attachment point.
- **Source & access.** NHC wind-probability products · USGS ShakeMap
  (PGA) · satellite flood extent. NHC/USGS already ingested (`nhc.py`,
  `usgs.py`); the new datum is a *trigger-band* metadata field.
- **Primitive + fallback.** H3 / Databricks Mosaic geospatial for radius-
  to-exposure proximity → local: store a `trigger_band` in item metadata;
  simple distance-to-attachment scalar.
- **Wiring target.** `regime.cat_load` (→ `post_major_event` when a trigger
  is near-breach) + a `cat_event` daily callout. Sketch:
  `pc_gold.trigger_proximity` over `pc_bronze.ingested_items` where
  `source IN ('nhc','usgs')` (see `gold.sql`). **No physical table this
  wave.**
- **Status.** Researched; view sketch only.

### Lead 8 — InsurTech Capital-Flow

- **Vital sign.** Gives the `ai_insurtech` topic *substance* — structured
  deal facts instead of broad-keyword PR — so the 35% per-topic share cap
  (`summarize.TOPIC_CAP_PCT`) stops being the only governor.
- **Source & access.** Funding-round + broker-M&A news already flowing
  through RSS/Google-News proxies; the new step is structured extraction
  (amount, stage, target, investors).
- **Primitive + fallback.** Vector Search + `ai_query()` for deal
  extraction → local: Ollama embeddings + MLX extraction.
- **Wiring target.** `summarize.TOPIC_CAP_PCT['ai_insurtech']` — let a
  substantiated deal (real `amount_usd`/`stage`) bypass or relax the cap,
  and feed `signals` materiality. Table: `pc_silver.capital_flows`.
- **Status.** **Shipped** — `capital_flows.extract_deal` (amount/round/stage) over
  the ai_insurtech queue; a substantiated deal (real $ amount) is excluded from
  the `TOPIC_CAP_PCT` share cap via `enforce_topic_caps_protected`. Offline-tested;
  behavior-preserving when no deal carries an amount.

### Lead 9 — Regulatory Burden Barometer

- **Vital sign.** Hardens `signals burden_intensity_boost` with a
  per-state burden velocity, the structured core of the Regulatory Sonar.
- **Source & access.** LegiScan API (free tier) bill velocity + SERFF
  filing volume (`ingest/serff.py`), bucketed per state.
- **Primitive + fallback.** `ai_query()` for bill-burden classification +
  Genie + Alerts → local: MLX classification; mechanical per-state count.
- **Wiring target.** `signals.BURDEN_INTENSITY_BOOST` + the new `state`
  dimension. Requires the **`state` column on `triage_verdicts`** (added
  to DDL this wave) so burden can be sliced per state. View:
  `pc_gold.burden_by_state` (shipped). The local mirror column +
  triage-prompt change to emit `state` is the follow-up.
- **Status.** **Shipped** — triage now emits a validated `state` on
  regulatory_rate items → `items.state`; `db.burden_by_state()` rolls up an
  intensity-weighted per-state pressure reading (`digest burden`). Tested. The
  LegiScan bill-velocity ingestor remains the documented follow-up.

### Lead 10 — Macro→Loss Transmission  **[VIEW SKETCH ONLY]**

- **Vital sign.** Hardens `macro_linkage` + `rates_cost_of_capital`. The
  cross-domain lead: connect macro-ai-digest's leading cost/rate signals to
  the PC topics they should move.
- **Source & access.** macro-ai-digest's `macro_bronze.fred_observations`
  + `macro_gold.topic_trend`, shared into the `digest` catalog.
- **Primitive + fallback.** Delta Sharing for cross-catalog access +
  `ai_forecast()` on the transmission lag → local: deferred (needs both
  digests sinking to the same warehouse).
- **Wiring target.** `signals._topic_relevance` under a macro-stress regime
  (lift `supply_chain`/`personal_lines`/`reserving` when a macro print
  fires). Sketch: `xdomain.macro_loss_transmission` joining `pc_*` ↔
  `macro_*` on a date × topic grain with a transmission LAG (see
  `xdomain.sql`). **No physical table this wave.**
- **Status.** Researched; view sketch only.

---

## Unifying surfaces

- **`pc_gold.market_ekg`** (shipped, `gold.sql`) — the panel view: one row
  per lead with `latest_value`, `zscore`, `trend`, `as_of`, `is_stale`.
  Leads 7 & 10 are not yet arms (sketch-only).
- **EKG Alerts** (shipped, `alerts.sql`) — one Alert per lead off the panel:
  fires on flatline (`is_stale`) or spike (|z| ≥ threshold / lead-specific
  level).
- **Genie space** (to create on the warehouse) — point a Genie space at
  `pc_gold.market_ekg` + the per-lead bronze/silver tables so the panel is
  queryable in natural language ("which vitals are abnormal this week and
  what topics did they move?"). No code; warehouse setup step.

## Source-access appendix

| Lead | Source | Access | Key needed |
|---|---|---|---|
| 1 | Guy Carpenter ROL index | Published commentary / scrape | — |
| 1 | Artemis Deal Directory | Free web | — |
| 1 | Lane Financial ILS reviews | Published quarterly | — |
| 2 | OpenFEMA Disaster Declarations | REST API | No |
| 2 | NOAA CPC outlooks | Free | No |
| 2 | US Drought Monitor | Free API/CSV | No |
| 2 | PowerOutage.us | Scrape / paid | Free snapshot |
| 3 | Manheim UVVI | Published monthly / scrape | — |
| 3 | FRED parts/labor/medical | **Already ingested** (`fred.py`) | FRED key |
| 4 | Marathon nuclear-verdict tracker | Published reports | — |
| 4 | Westfleet TPLF survey | Annual report | — |
| 4 | CourtListener dockets | **Already ingested** | `COURTLISTENER_TOKEN` |
| 5 | EDGAR filings | **Already ingested** (`edgar.py`) | UA string |
| 6 | Investor-supplement PDFs | **Already ingested** (`investor_supp.py`) | — |
| 6 | NAIC Schedule P | `naic_schedp.py` (scaffold) | — |
| 7 | NHC wind-prob / USGS ShakeMap | **Already ingested** | No |
| 8 | Funding/M&A news | **Already ingested** (RSS/GN) | — |
| 9 | LegiScan API | Free tier | API key |
| 9 | SERFF filings | `serff.py` (scaffold) | — |
| 10 | macro-ai-digest medallion | Delta Sharing | Warehouse |

## What shipped in this wave (offline)

- **Phase A** — this document.
- **Phase B** — bronze/silver/gold/alerts/xdomain DDL for the leads above
  (physical tables for 1,2,3,4,5,8 + `state` col & `burden_by_state` for 9;
  `market_ekg` panel + EKG Alerts; sketches for 7 & 10) and the matching
  no-op `DatabricksSink` writers.
- **Phase C** — Lead 6 prototype: `parse.triangles`, `investor_supp`
  triangle routing, and end-to-end tests.

## Follow-ups for the user (Mac mini / warehouse)

- Validate `investor_supplements.yaml` URL templates against live quarterly
  PDFs; flip insurers to `enabled:true` (start with PGR/ALL/TRV) and run
  `digest ingest --source investor_supp && digest reserving && digest
  signals` to light up `reserve_deterioration_boost` on real data.
- Apply the medallion DDL on the warehouse (`bronze.sql`, `silver.sql`,
  `gold.sql`, `alerts.sql`, `xdomain.sql`) and create the per-lead Alerts +
  a Genie space over `pc_gold.market_ekg`.
- Implement each lead's ingestor (sequencing: 6→5→1+2→rest), wiring its
  reading into the documented `regime`/`signals` symbol.
