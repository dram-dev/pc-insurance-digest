# pc-insurance-digest — Claude context

User nickname for this project: **"PC Digest"** (or "the PC Digest"). The
sibling project is **macro-ai-digest** ("the macro digest"), at
[dram-dev/macro-ai-digest](https://github.com/dram-dev/macro-ai-digest).

## What this project is

A curated daily + weekly digest covering **US P&C insurance and financial
services**. Built by copy-modify from `macro-ai-digest` to deliberately
prove out a second concrete domain — the divergence between the two will
later define the seams for a shared `digest-core` framework. **Do not
pre-design the framework**; wait until Wave 2 of PC Digest is done and
the actual divergence is visible.

Pipeline shape:
```
ingest → triage (Ollama Qwen2.5:14b) → summarize (MLX Qwen3.5-27B) → publish (Obsidian)
```

Both Ollama and the MLX server are **shared with macro-ai-digest** and
run as launchd jobs managed by that project. PC Digest only writes its
own `com.dr.pcdigest.*` jobs.

## Current state — Wave 1 shipped

| Component | Status |
|---|---|
| Repo + scaffold (copy-modify from macro-ai-digest) | ✅ |
| 17-topic P&C taxonomy in triage + summarize | ✅ |
| 14-insurer EDGAR universe + Python auto-keep hook | ✅ |
| Trade-press RSS (Insurance Journal, Reinsurance News, Artemis, Carrier Mgmt) + Google News proxies (NHC proxy superseded by direct `nhc` ingestor in Wave 2) | ✅ |
| Reddit (r/Insurance, r/Actuary, r/CFP, weather/EQ) + Substack + HN | ✅ |
| 35% per-topic cap on `ai_insurtech` (configurable in `summarize.py` → `TOPIC_CAP_PCT`) | ✅ |
| Obsidian publish to `81 P&C Digest/{Daily,Topics,Weekly,_meta}` | ✅ |
| launchd jobs loaded: `am` 04:00, `pm` 16:00 daily, `weekly` Sat 06:00 | ✅ |

Each is committed on `master` and pushed to
[github.com/dram-dev/pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest).

## Wave 2 / 3 roadmap

**Wave 2:**

| Component | Status |
|---|---|
| NHC advisory ingestor (`nhc.py`) — Atlantic/E.Pac/C.Pac RSS, U.S./Caribbean-threat filter + Python auto-keep (score=1.0) | ✅ |
| USGS earthquake ingestor (`usgs.py`) — M4.5+ GeoJSON, fetch-filtered to M≥5.0; Python auto-keep M≥6.0 (score=0.95) | ✅ |
| SPC ingestor (`spc.py`) — watch/warning RSS, filtered to tornado/severe thunderstorm/enhanced+ | ✅ |
| NIFC ingestor (`nifc.py`) — WFIGS ArcGIS API (InciWeb RSS dead); active wildfires ≥1000 ac, <100% contained | ✅ |
| Market-cycle regime detector (`regime.py`) — Qwen3.5 cycle judgment over 60d window + mechanical cat_load; 72h cadence; 2-recompute hysteresis; `config/regime_override.yaml` override | ✅ |
| Signal leaderboard (`signals.py`) — `source × regime × topic_relevance × recency × llm_judgment × topic_priority_boost × burden_intensity_boost`; top-5 daily, top-15 + per-source quality weekly | ✅ |
| Regulatory Sonar lite — `burden_direction` / `burden_intensity` triage fields on `regulatory_rate` items, `burden_intensity_boost` in leaderboard scoring, daily-note callout on high-intensity items. See "Regulatory Sonar" below. | ✅ |

**Wave 2.x (shipped 2026-05-24 from Score Higher review):**

| Component | Status |
|---|---|
| 9 new state-DOI / FAIR Plan / trade-body Google News RSS proxies in `config/rss_feeds.yaml` (CA CDI, FL FLOIR, TX TDI, NY DFS, LA LDI, FAIR Plan, personal-auto rates, homeowners rates, APCIA/NAMIC, Triple-I) | ✅ |
| FRED ingestor — 7 P&C loss-cost CPI/PPI series with ±1.5σ anomaly gate; auto-keep via `auto_keep_quantitative()`; topic locked to `supply_chain` | ✅ |
| Industry-research Google News proxy (LexisNexis Risk Solutions + JD Power) | ✅ |
| Scaffolded ingestors registered + no-op safe: `courtlistener`, `collision`, `state_doi` | ✅ |
| Industry-research direct scraper (`industry_research.py`) — Phase 2 LexisNexis + JD Power; config-driven with per-source enabled flag; topic=`personal_lines` | ✅ |

**Wave 3 Phase 1 (shipped 2026-05-25):**

| Component | Status |
|---|---|
| Triage prompt tightening — auto-discard rules for non-U.S./Caribbean tropical cyclones (Chinese NHC pattern), generic travel-volume reporting, road/infrastructure funding without insurance linkage, and general AI/LLM vendor PR without insurance use case | ✅ |
| `google_news_insurtech` narrowed — requires insurance-domain qualifier (insurance/insurer/carrier/underwriter/MGA/broker) alongside the AI/tech term; reduces reliance on the 35% `ai_insurtech` share cap | ✅ |
| 4 industry-economics GN proxies (`google_news_{ft,economist,wsj,bloomberg}_insurance`) — site-scoped queries with insurance keywords + Lloyd's for London-market coverage; 14d window; topic_hint=`macro_linkage` | ✅ |
| **Databricks medallion DDL** — `packages/digest-core/sql/databricks/{bronze,silver,gold}.sql`. Bronze: ingested_items (incl. drops, partitioned by source), fred_observations (full series), regime_signals, pipeline_telemetry. Silver: triage_verdicts, signal_scores (all 10 boost factors as columns), summaries, manual_ratings (Wave 4 placeholder). Gold: latest_scores helper + daily/weekly leaderboards, source_quality, score_calibration, regime_history, pipeline_slos. Join key: `item_hash = sha256(source || '::' || source_id)` derived at sink-write time | ✅ |
| **`DatabricksSink` scaffold** — `src/digest/sinks/databricks.py` + `src/digest/sinks/__init__.py`. Lazy connection, lazy import of `databricks-sql-connector` (optional dep). Best-effort writes: errors logged + swallowed; SQLite remains source of truth. No-op when `DATABRICKS_ENABLED=false` (default). Settings: `DATABRICKS_{ENABLED,HOST,HTTP_PATH,TOKEN,CATALOG}` | ✅ |
| **Sink wired into `db.py`** — 7 write checkpoints: `upsert_items` → bronze.ingested_items; `log_run` → bronze.pipeline_telemetry (stage=ingest); `update_triage` → silver.triage_verdicts; `update_summary` → silver.summaries; `log_summarizer` → bronze.pipeline_telemetry (stage=summarize); `upsert_regime_signal` → bronze.regime_signals; `upsert_signal_scores` → silver.signal_scores. Sink calls run AFTER each `with get_conn()` commits, so SQLite is durable before any Databricks interaction. Caller signatures unchanged | ✅ |

**Wave 3:**

*Liability Intelligence cluster (new — highest user priority):*

| Item | Detail |
|---|---|
| **Verdict / docket tracker** | ✅ `src/digest/ingest/courtlistener.py` — real fetch loop (tier1 + emerging + tier3); P&C NOS filter (365/360/385/480/870); 12s sleep, 100-req daily cap; auto-keep hook → `social_inflation` (score=0.85). Needs COURTLISTENER_TOKEN env var. Federal circuits tier skipped (appeals dockets). TODO: add MDL keyword filter to reduce noise. |
| **TPLF dedicated ingestor** | Sources: PACER court RSS (via CourtListener), ILR/ATRA advocacy publications, Law360/Bloomberg Law RSS, LegiScan state disclosure bills (shared LegiScan client with Regulatory Sonar). Promote `litigation_tplf` sub_tag to a first-class leaderboard boost factor. |
| **Claims / actuarial dataset** | (c) ✅ CCC/Mitchell quarterly collision reports — `src/digest/ingest/collision_data.py`; BeautifulSoup multi-selector scraper; `supply_chain` topic; TODO: validate CSS selectors against live pages on Mac mini (curl blocked in cloud). (a) NAIC Schedule P triangles — pending. (b) Insurer investor supplement PDFs — pending. |
| **State DOI direct scrapers** | ✅ `src/digest/ingest/state_doi.py` — full BeautifulSoup `_scrape_state()` implementation; config in `config/state_doi_sources.yaml`; all 5 states (CA/FL/TX/NY/LA) remain `enabled:false` pending CSS selector validation on Mac mini. auto-keep hook → `regulatory_rate` (score=0.9). TODO: curl each state URL to confirm selector, flip `enabled:true` CA first. |

*Source expansion (carried over):*

| Item | Detail |
|---|---|
| **AM Best rating actions** | Currently `site:ambest.com` Google News proxy. Try direct RSS with a real browser `User-Agent`; Radware-blocked on default UA. |
| **NAIC + state DOI / SERFF** | ✅ Wave 3 Phase 2 — `src/digest/ingest/serff.py` + `config/serff_states.yaml` scaffold; 5 states (CA/FL/TX/NY/LA) with per-state `enabled:false` flags. Portal dispatch (serff_standard vs cdi_prior_approval vs floir_irfa). ≥5% requested-change filter + LOB whitelist (personal auto / homeowners / commercial auto / dwelling / umbrella). Auto-keep hook → `regulatory_rate` (0.9 base, 0.95 when \|Δ\|≥10%). TODO: validate selectors on Mac mini and implement POST + search params per portal — naive GET currently returns the landing page for SERFF standard. |
| **Lloyd's / Bermuda** | Artemis ILS data + syndicate results. Feeds `reinsurance_cycle` and `rates_cost_of_capital`. |

*Pipeline quality (carried over):*

| Item | Detail |
|---|---|
| **Triage prompt tightening** | ✅ Wave 3 Phase 1 — Chinese NHC, travel-volume, road-funding, AI-vendor-PR auto-discard rules shipped in `triage.py` SYSTEM_PROMPT. |
| **Regulatory Sonar full** | `src/digest/regulatory_sonar.py` periodic detector (3-day cadence), LegiScan API ingestor for state bills, per-state burden-pressure index, weekly note section, daily callout on trend-fire. See "Regulatory Sonar" below. Wave 3 Phase 1 plan defers this to Wave 4 — waits for SERFF + LegiScan signal density. |

After Wave 2 lands, extract the shared core into a `digest-core`
framework package; PC Digest and macro-ai-digest become thin domain
plug-ins. Trigger: all 3 Wave 2 items shipped + **1 week max** of daily
dogfooding before cutting seams.

**Scaffolded:** `packages/digest-core/` holds the empty package skeleton
and [EXTRACTION_PLAN.md](packages/digest-core/EXTRACTION_PLAN.md) — a
concrete what-moves-where map (definite-core / definite-domain / tricky
seams) authored while Wave 2 divergence is fresh. The actual code lift
waits for the dogfooding window to close.

## Locked design decisions

### Topic taxonomy (17 topics + 1 sub-tag)

Canonical list lives in [src/digest/triage.py](src/digest/triage.py)
`TOPICS` and must stay in sync with `valid_topics` in
[src/digest/summarize.py](src/digest/summarize.py) and `TOPIC_LABELS`,
`TOPIC_CALLOUT`, `TOPIC_EMOJI`, `TOPIC_ORDER` in
[src/digest/obsidian.py](src/digest/obsidian.py).

1. `cat_event` — active named storms, EQ, severe convective storms, wildfire, flood
2. `reinsurance_cycle` — 1/1, 4/1, 7/1 renewals; capacity; retro; ILS
3. `regulatory_rate` — state DOI rate filings (SERFF), NAIC actions, FIO/Treasury
4. `underwriting_results` — combined ratio, loss/expense ratios
5. `reserving` — adverse/favorable development, IBNR, asbestos/PFAS
6. `ma_capital` — insurer M&A, IPO, raises, buybacks, dividends
7. `climate_risk` — physical & transition risk, ESG, market exits (CA/FL/LA)
8. `cyber` — cyber insurance market, breach impact, AI as attack surface
9. `social_inflation` — nuclear verdicts, severity, tort reform
   *(sub-tag `litigation_tplf` for TPLF funders, MDLs, attorney economics)*
10. `ai_insurtech` — AI in UW/claims, insurtech funding, MGAs
11. `distribution` — broker M&A (MMC/AON/WTW/BRO/AJG/RYAN), agency networks
12. `personal_lines` — auto/home pricing, telematics, market exits
13. `commercial_specialty` — E&S, workers comp, D&O, E&O, environmental
14. `macro_linkage` — CPI→loss costs, FX, geopolitics, energy→CAT
15. `rates_cost_of_capital` — rate impact on investment income, debt, cat bonds
16. `supply_chain` — auto parts/labor, contractor capacity, medical/Rx
17. `analytics_modeling` — cat models (RMS/AIR/Verisk/KCC), pricing, CAS

### Source multipliers (signal scoring)

| Source | Mult |
|---|---:|
| EDGAR 8-K (insurers) · NOAA/NHC active advisories | **1.3** |
| AM Best · State DOI (SERFF) · NAIC | **1.2** |
| Lloyd's / Bermuda updates | **1.1** |
| Insurance Journal · Reinsurance News · Artemis · Carrier Mgmt | **1.0** |
| WSJ/FT/Bloomberg insurance desk · Substack | **0.9** |
| Reddit (r/Insurance etc.) | **0.7** |
| HN | **0.6** |

### Triage prompt rules (locked)

- **Hybrid auto-keep:** Python at triage entry hard-enforces EDGAR 8-K/10-K/10-Q
  from the 14 named insurer tickers (cannot silently fail on material disclosures).
  All other auto-keep rules live in the prompt for the model to handle in context.
- **sub_tags is a list of strings** (`[]` or `["litigation_tplf"]`) — future-proof
  schema avoids migration when new sub-tags are added.
- **`reason` ≤ 50 words** — enough room for judgment calls without bloat.

### Per-topic share caps

Lives in [src/digest/summarize.py](src/digest/summarize.py) `TOPIC_CAP_PCT`:
```python
TOPIC_CAP_PCT = {"ai_insurtech": 0.35}
```
Without this cap, broad-keyword Google News feeds drown out substantive
P&C content. Items dropped by the cap remain triage=keep and appear in
the kept-unsummarized section of the daily note. Add new caps by editing
this dict.

### Topic priority emphasis (locked — user preference)

**Personal lines auto + homeowners/fire is the highest-priority topic
signal. Liability trends (social inflation, commercial specialty, reserving)
and inflation cost-driver feeds (supply chain) are boosted above personal
lines to keep them from being buried by cat_event volume.** This applies
across the pipeline:

- **Scoring (Wave 2 leaderboard):**
  ```python
  topic_priority_boost = {
      "personal_lines":       1.3,
      "social_inflation":     1.4,   # nuclear verdicts, tort reform, TPLF
      "commercial_specialty": 1.4,   # GL, WC, D&O/E&O, E&S
      "reserving":            1.4,   # adverse dev, IBNR, long-tail
      "supply_chain":         1.4,   # auto parts, construction, labor, medical/Rx
      "underwriting_results": 1.2,   # combined ratio, AY commentary, industry profitability
      "distribution":         1.2,   # broker M&A (MMC/AON/WTW/BRO/AJG/RYAN/Patriot)
      "regulatory_rate":      1.2,   # state DOI / SERFF / NAIC (stacks with burden_boost)
  }
  ```
  applied alongside four additional cross-cutting factors (Wave 2.x + Wave 3 Phase 2):

  ```python
  insurer_priority_boost = {   # EDGAR items only, keyed on metadata.ticker
      "PGR": 1.5, "ALL": 1.5, "BRK": 1.5,   # personal-auto big-3 (BRK = GEICO parent)
      "TRV": 1.3, "CB": 1.3,
      "HIG": 1.2, "AIG": 1.2,
  }
  inflation_keyword_boost = 1.2  # title/summary names an inflation driver: auto
                                 # parts, construction cost, labor cost/supply,
                                 # verdict/judgement/settlement, tort reform,
                                 # severity/loss cost, body shop, repair cost
  regulatory_action_boost = 1.2  # title/summary names a state DOI action,
                                 # insurer of last resort (FAIR Plan, Citizens),
                                 # SERFF rate filing, NAIC adoption, NYDFS/CDI/
                                 # FLOIR/TDI/LDI bulletin, tort-reform bill
  litigation_tplf_boost  = 1.3   # Wave 3 Phase 2 — fires when (a) the LLM tags
                                 # sub_tags=['litigation_tplf'] OR (b) title/
                                 # summary names TPLF/MDL/nuclear-verdict
                                 # signals. Stacks on top of social_inflation's
                                 # 1.4× topic_priority_boost → 1.82× combined.
  ```

  Final formula:
  `score = source × regime × topic_relevance × recency × llm_judgment × topic_priority_boost × burden_intensity_boost × insurer_priority_boost × inflation_keyword_boost × regulatory_action_boost × litigation_tplf_boost × reserve_deterioration_boost`.

  `reserve_deterioration_boost` (Databricks Option 5) fires when an item names an
  insurer with adverse reserve development in `reserving_signals`:
  `min(1 + adverse_IBNR_deterioration, 1.3)`. **Neutral (1.0) until `digest
  reserving` produces data** (reads `db.reserving_severity_map()`), so the
  formula is behaviour-preserving until loss-triangle ingestion is live.

  Separately, **`learned_score`** (Databricks Option 4) is persisted on each
  `signal_scores` row alongside the heuristic `score` whenever a trained model
  exists (`digest learn`); it does **not** affect ranking — the heuristic stays
  authoritative — it rides along for the A/B (`gold.score_calibration` /
  `outcome_hit_rate`). NULL until a model is trained.

  **User-editable weights** (Wave 3 Phase 4, shipped 2026-05-25): the
  boost VALUES above are now defaults — the user can override any of
  them by editing the YAML frontmatter of
  `${OBSIDIAN_VAULT_PATH}/81 P&C Digest/_meta/Scoring Weights.md`.
  `signals.py` re-reads the file on each `digest signals` run when its
  mtime changes (cached otherwise). Missing file or malformed YAML →
  fall back to defaults silently with a warning. Unknown keys ignored.
  Sections: `sources`, `topics`, `insurer_priority`, `keyword_boosts`,
  `burden_intensity`. Regex patterns for the keyword boosts stay
  code-side; only the boost VALUES are tunable.

- **LLM materiality anchors** (`summarize.py` SYSTEM_PROMPT, sharpened
  2026-05-24 after Score Higher review): the 1.5 tier now explicitly
  requires industry-wide records ("biggest in N years"), top-5-state
  DOI rate actions ≥10%, FAIR Plan / Citizens actions, tort-reform
  passage, nuclear verdicts ≥$50M, or reinsurer capital events ≥$500M.
  Prompt anchors the LLM to ERR HIGH on systemic signals.
- **Topic ordering:** `personal_lines` lifted near top of `TOPIC_ORDER` in
  [src/digest/obsidian.py](src/digest/obsidian.py). Only `cat_event`
  precedes it, and only when regime = `post_major_event` or `active_season`.
- **Triage prefer-keep:** prompt explicitly favors personal auto pricing,
  homeowners pricing, wildfire-driven market exits (CA/FL/LA),
  telematics rollouts, FAIR Plan / state insurer-of-last-resort changes,
  fire-line reinsurance.
- **LLM materiality (Wave 2):** Qwen3.5 judgment prompt weights personal
  auto + homeowners/fire as high-relevance.
- **Ingest gap-check:** state DOI bulletins (CA, FL, LA), state
  insurer-of-last-resort feeds, APCIA/NAII statements. Likely needs
  Google News `site:` proxies until SERFF (Wave 3).

**Fire-content topic routing** (avoid duplication):
- Wildfire as event (acres, evac, deaths) → `cat_event`
- Wildfire's market response (exits, rate hikes, FAIR Plan growth) → `personal_lines`
- Long-run physical risk / ESG framing → `climate_risk`

### Regulatory Sonar (Wave 2 lite + Wave 3 full)

Legislative and regulatory environment is extremely impactful in P&C.
The sonar continuously detects **negative oversight trends that put
burdens on insurers** — rate suppression, expanded claims liability
statutes, mandated coverage, anti-redlining underwriting restrictions,
climate mandates, FAIR Plan assessment expansions, federal encroachment.

**Schema (Wave 2 lite):**
Two new triage output fields, populated by LLM for `regulatory_rate`
items only (null elsewhere):
- `burden_direction`: `increasing | neutral | decreasing`
- `burden_intensity`: `high | medium | low`

These are proper columns (3-way classifications, not flags), so they
ship with a one-time SQLite migration.

**Scoring (Wave 2 lite):**
Adds `burden_intensity_boost` as the last factor in the leaderboard
formula — `{high: 1.3, medium: 1.1, low: 1.0, null: 1.0}`. High-intensity
oversight items rank above routine filings.

**Detector (Wave 3 full — `src/digest/regulatory_sonar.py`):**
- Cadence: 3 days, triggered from AM job if last run > 72h.
- Reads trailing 90d of `regulatory_rate` items + their burden tags.
- Computes per-state + federal **burden pressure index**
  (intensity-weighted count).
- Trend fires when a state's 30d window exceeds its 90d baseline.
- LLM judgment confirms significance (no fixed-baseline noise).

**Sources for sonar ingest:**
- LegiScan API (free tier) — state bill metadata
- State DOI bulletins (CA, FL, LA priority)
- NAIC committee minutes + actions
- NCOIL bulletins
- APCIA / NAMIC trade-body alerts
- Federal: FIO, Treasury, CFPB

Scope is US state + federal only. No international.

**Output surfaces:**
- Daily note: one-liner callout when a high-intensity item is ingested
  (Wave 2 lite); fuller callout when sonar detector fires (Wave 3).
- Weekly note: "Regulatory Sonar — top burden trends, by state, with
  citations" section (Wave 3).

### Regime concept (Wave 2, two-dimensional)

PC Digest has two regime axes (vs. macro digest's one):
- **Market cycle:** hard_market (1.20×) · transitioning_to_hard (1.10×) ·
  stable (1.00×) · transitioning_to_soft (0.95×) · soft_market (0.85×)
- **CAT load:** low_season (1.00×) · active_season (1.10×) · post_major_event (1.20×)
- Combined regime multiplier = `market_cycle × cat_load`

Detector inputs (when implemented): combined-ratio trend, capacity narrative
from trade press, ILS pricing, active NHC advisories, recent EQ M ≥ 6.0.

### MLX scheduling (no contention with macro digest)

| Job | Macro time | PC Digest time |
|---|---|---|
| `am` daily | 01:00 | **04:00** |
| `pm` daily | 13:00 | **16:00** |
| `weekly` | Fri 19:00 | **Sat 06:00** |

Both projects POST to the single shared `mlx_lm.server` on localhost:8080
(managed by macro digest's `com.dr.mlx.server` launchd job, KeepAlive).
Stagger is 3 h on daily, full overnight gap before the weekly.

Future bulletproof deconfliction (deferred): lockfile at `/tmp/mlx.lock`
checked in `summarize.py`.

### Insurer ticker universe (Wave 1 + BRK)

Lives in [config/edgar_tickers.yaml](config/edgar_tickers.yaml) AND as
the Python set `INSURER_TICKERS_WAVE1` in
[src/digest/triage.py](src/digest/triage.py). **Keep them in sync** —
the triage Python auto-keep hook reads the Python set directly.

TRV · ALL · PGR · CB · HIG · AIG · MET · PRU · RNR · EG · AXS · MMC · AON · WTW · BRK

BRK (Berkshire Hathaway) was added in Wave 2.x to cover GEICO via the
parent's consolidated filings. The insurer-priority boost treats BRK at
1.5× (same as PGR/ALL) since GEICO is a personal-auto big-3 carrier.

### EDGAR auto-keep behavior

The Python hook (`db.auto_keep_insurer_filings`) auto-keeps every 8-K /
10-Q / 10-K from the named ticker universe and **locks the topic at
triage time** so the summarizer can't reclassify a content-less filing
as `ai_insurtech`:

| Form    | Locked topic           |
|---------|------------------------|
| 8-K     | `underwriting_results` |
| 10-Q    | `underwriting_results` |
| 10-K    | `underwriting_results` |
| 13F-HR  | `ma_capital`           |

`src/digest/ingest/edgar.py` fetches body content for 8-K (EX-99.1 press
release) and 10-Q/10-K (primary doc head, 5K chars) within a 21-day
cutoff. Older filings reach summarize.py with empty content; the
`_maybe_stub_insurer_filing` short-circuit emits a deterministic stub
(materiality 0.9, confidence low) instead of calling MLX — preventing
the "...no body content..." hallucination pattern.

### Obsidian output

Vault is **shared with macro digest** at `OBSIDIAN_VAULT_PATH` (set in
`.env`). PC Digest writes to **`81 P&C Digest/`** (Johnny Decimal sibling
to macro digest's `80 Digest/`).

Folder layout:
- `Daily/YYYY-MM-DD.md` — one file per day, regenerated each publish
- `Topics/<Topic Label>.md` — one file per topic with summarized items,
  upserted by ID
- `Weekly/<YYYY-WW>.md` — Saturday weekly rollup (Wave 1: items grouped
  by topic; Wave 2 will add synthesis)
- `_meta/Run Log.md` — append-only operations log

## Known issues / debt

- **6 trade-press RSS feeds dead or bot-blocked** (insurance_erm,
  pc360, trading_risk, intelligent_insurer, am_best_news, naic_news).
  Wave 1 replaced them with Google News `site:` and keyword proxies. AM
  Best especially worth revisiting with a real browser UA.
- **Triage prompt is too permissive on edge cases**: Chinese NHC (not
  Hurricane Center) slipped through, generic travel-volume articles,
  road-funding policy, AI model PR. Hand-curated dropouts have been
  applied; systemic fix in Wave 2.
- **`AI & Insurtech` skews high** even with the 35% cap. The
  `google_news_insurtech` query is broad — narrow it for Wave 2 (require
  insurance-specific qualifier, exclude general AI model releases).
- **Weekly note is bare**: Wave 1 just groups items by topic. No
  synthesis, no themes, no must-reads. Wave 2 reintroduces these.

## Architecture quick-reference

```
src/digest/
├── cli.py        # Click commands: ingest, triage, summarize, regime, signals,
│                 # pipeline, publish, weekly, stats, recent, health, init-db
├── config.py     # pydantic-settings; reads .env
├── db.py         # SQLite schema + queries; shares schema with macro for portability
├── triage.py     # P&C system prompt, 17-topic enum, Python auto-keep hook for EDGAR 8-K
│                 # + Wave 2 lite Regulatory Sonar burden_direction/intensity fields
├── summarize.py  # MLX summarizer; per-topic share cap; P&C reader persona prompt
│                 # + Wave 2 materiality field for leaderboard llm_judgment
├── regime.py     # Wave 2 two-axis regime detector (market_cycle × cat_load)
├── signals.py    # Wave 2 leaderboard formula + per-item score persistence
├── obsidian.py   # Markdown writer; 17-topic label/callout/emoji dicts; daily + weekly + topic archives
│                 # + Wave 2 regime callout, top-N leaderboard, sonar one-liner
├── health.py     # Launchd job status + DB stats
├── security.py   # secrets scan + file-perm audit
└── ingest/
    ├── base.py        # IngestorBase, IngestedItem dataclass
    ├── rss.py
    ├── edgar.py
    ├── reddit.py
    ├── substack.py
    ├── hackernews.py
    ├── nhc.py         # Wave 2 — NHC tropical cyclone RSS (US/Caribbean threat filter)
    ├── usgs.py        # Wave 2 — USGS M ≥ 5.0 earthquake GeoJSON
    ├── spc.py         # Wave 2 — SPC convective outlook RSS
    ├── nifc.py        # Wave 2 — NIFC WFIGS active wildfire ArcGIS REST
    ├── fred.py               # Wave 2.x — FRED CPI/PPI cost-driver anomalies (live)
    ├── courtlistener.py      # Wave 3 — federal MDL docket tracker; 11 NOS codes + 29 MDL keywords; needs COURTLISTENER_TOKEN
    ├── collision_data.py     # Wave 3 — CCC + Mitchell quarterly reports; TODO validate CSS selectors on Mac mini
    ├── state_doi.py          # Wave 3 — direct state DOI press scrapers; all states enabled:false; TODO validate selectors + enable CA first
    ├── industry_research.py  # Wave 3 — LexisNexis Risk + JD Power direct scraper; config/industry_research_sources.yaml; all disabled pending selector validation
    └── serff.py              # Wave 3 Phase 2 — state SERFF rate filings ≥5%; portal dispatch (serff_standard / cdi_prior_approval / floir_irfa); all states enabled:false pending selector + POST validation

src/digest/sinks/      # Wave 3 Phase 1 — secondary write destinations
├── __init__.py        # exports module-level `sink` singleton
└── databricks.py      # DatabricksSink (no-op when DATABRICKS_ENABLED=false)
                       # 7 write methods: ingested/fred/regime/telemetry/triage/score/summary
                       # lazy import + lazy connection + best-effort writes

packages/digest-core/sql/databricks/   # Wave 3 Phase 1 — medallion DDL
├── bronze.sql         # 4 tables — raw firehose incl. drops + full FRED + telemetry
├── silver.sql         # 4 tables — triage / scores (10 boost factors) / summaries / manual_ratings
└── gold.sql           # helper + 6 curated views — leaderboards / source_quality / SLOs
```

## Sample next-feature prompts

Pick one of these from a mobile session and Claude will have enough
context from this file to act:

- **Tighten the triage prompt.** "Add auto-discard rules in triage.py for
  the patterns we hand-curated yesterday: Chinese NHC (require
  'U.S./Caribbean threat'), generic travel-volume reporting, road-funding
  policy, AI model PR. Show me the prompt diff before committing."

- **Narrow the insurtech Google News query.** "The `google_news_insurtech`
  feed pulls in too much general AI/SaaS content. Rewrite the query to
  require an insurance-context qualifier (e.g., `insurance OR insurer OR
  carrier OR underwriter`) and exclude unrelated AI model releases."

- **NOAA/NHC ingestor (Wave 2).** "Implement
  `src/digest/ingest/nhc.py` that fetches the NHC public advisory RSS
  feeds and emits IngestedItem rows when there's an active named storm
  with U.S./Caribbean threat. Add the corresponding Python auto-keep
  hook in triage.py and wire it into the pipeline."

- **Market-cycle regime detector (Wave 2).** "Implement
  `src/digest/market_regime.py` that infers the current market cycle
  position (hard/soft/transitioning) from combined-ratio trends and
  trade-press capacity narrative. Two-axis: cycle × cat-load."

- **Tighten the weekly note.** "Port the weekly synthesis from
  macro-ai-digest's `weekly.py` and add it to PC Digest. P&C reader
  persona, themes + must-reads + contrarian signal, no macro-AI
  intersection section."

## How to run locally (Mac mini setup)

```bash
cd ~/Projects/pc-insurance-digest
uv sync
cp .env.example .env  # fill in OBSIDIAN_VAULT_PATH, EDGAR_USER_AGENT, REDDIT_*
uv run digest init-db
uv run digest pipeline --run-type manual  # smoke test
bash scripts/install_launchd.sh           # when ready to schedule
launchctl list | grep com.dr.pcdigest
```

For mobile / cloud Claude Code sessions, the codebase is fully working
from this repo — but the live MLX/Ollama servers and Obsidian vault are
on the user's Mac mini and not reachable. Develop code; user runs it
locally.
