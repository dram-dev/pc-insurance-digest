# P&C Insurance & Financial Services Digest

Daily + weekly curated digest covering US P&C insurance and financial services.
Sibling to [macro-ai-digest](../macro-ai-digest); shares MLX/Ollama backends and
the same Obsidian vault (lands in `81 P&C Digest` next to `80 Digest`).

For the full design context (locked decisions, scoring formula, regime axes,
Regulatory Sonar, etc.), see [CLAUDE.md](CLAUDE.md).

## Status (Waves 1–3 shipped · digest-core extraction underway)

**Pipeline:** `ingest → triage (Ollama Qwen2.5:14b) → summarize (MLX Qwen3.5-27B
local) → score (signals leaderboard) → publish (Obsidian)`

**Ingestors (live):**
- **EDGAR** — 15-insurer universe (TRV, ALL, PGR, CB, HIG, AIG, MET, PRU, RNR,
  EG, AXS, MMC, AON, WTW, BRK); 8-K/10-Q/10-K body fetch; Python auto-keep
- **Trade press** — Insurance Journal, Reinsurance News, Artemis, Carrier
  Management + Google News `site:` proxies for FT / Economist / WSJ /
  Bloomberg insurance desks
- **Cat events** — NHC tropical cyclone (U.S./Caribbean threat filter), USGS
  M≥5.0 earthquakes (M≥6.0 U.S./territory auto-keep), SPC severe-weather
  outlooks, NIFC active wildfires ≥1000 ac
- **FRED** — 7 P&C cost-driver CPI/PPI series with ±1.5σ anomaly gate
- **CourtListener** — federal MDL docket tracker (tier-1 + emerging
  jurisdictions, P&C NOS filter, MDL keyword auto-keep)
- **Reddit / Substack / Hacker News** — r/Insurance, r/Actuary, r/CFP,
  weather/EQ subreddits; Insurance Insider, Coverager; HN ≥100 points
- **Scaffolded, selector-validation pending on Mac mini:** state DOI direct
  scrapers (CA/FL/TX/NY/LA), SERFF rate filings ≥5%, CCC/Mitchell collision
  data, LexisNexis Risk + JD Power industry research, NAIC Schedule P,
  per-insurer investor supplements

**Triage / summarize / score:**
- 17-topic P&C taxonomy + `litigation_tplf` sub-tag (canonical list in
  [src/digest/triage.py](src/digest/triage.py))
- Hybrid auto-keep — Python enforces material categories (insurer 8-K/10-Q/10-K,
  NHC advisories, U.S. M≥6.0 quakes, FRED anomalies, CourtListener MDLs, state
  DOI bulletins, SERFF ≥5%, investor supplements, NAIC Schedule P); model
  handles the rest
- Two-axis regime detector — `market_cycle × cat_load`, 72h cadence with
  override file
- Signal leaderboard — 11-factor score `source × regime × topic_relevance ×
  recency × llm_judgment × topic_priority × burden_intensity × insurer_priority
  × inflation_keyword × regulatory_action × litigation_tplf`. All boost values
  are user-editable from the Obsidian vault — see _meta/Scoring Weights.md_
- Conviction tier — each scored item is tagged 🔴 high / 🟡 medium / 🔵 low by
  leaderboard score (thresholds in _meta/Scoring Weights.md → `signal_tiers`)
  and shown as a badge on the daily + weekly leaderboards; persisted to
  `signal_scores.tier` (and the Databricks silver layer)
- Regulatory Sonar **lite** — `burden_direction` / `burden_intensity` on
  `regulatory_rate` items, with leaderboard boost and a daily-note callout on
  high-intensity items

**Publish:** Daily + weekly notes + per-topic archives in
`{vault}/81 P&C Digest/{Daily,Topics,Weekly}/`, plus a `_meta/` folder for
operations log, scoring weights, and feedback files.

**Optional Databricks medallion sink** — bronze / silver / gold DDL ships in
`packages/digest-core/sql/databricks/`. `DatabricksSink` (implemented in
`digest_core.sinks.databricks`, wired through `src/digest/sinks/`) is
best-effort + lazy-connected and no-ops unless `DATABRICKS_ENABLED=true`;
SQLite remains source of truth.

## Shared core (`digest-core`)

The pipeline's domain-agnostic mechanics live in a uv-workspace package,
`packages/digest-core/` (`digest_core`), with PC Digest as a thin domain layer
on top. **The sibling [macro-ai-digest](https://github.com/dram-dev/macro-ai-digest)
now runs on the same core** (consumed via a path dep) — the second concrete
domain that validates the seams. Lifted so far: the SQLite base schema + CRUD,
`IngestorBase` (+ the RSS/Substack/HN/Reddit/EDGAR fetch logic), the summarizer
backends + JSON-repair / share-cap runner, the Obsidian render primitives /
`Paths` / topic-index block, and the CLI ingest mechanics. PC keeps the
P&C-specific config, taxonomy, prompts, scoring weights, and auto-keep rules. A
hermetic `pytest` suite (`tests/`) covers the lifted surface.

**Sources grow organically.** Every `IngestorBase` subclass self-registers (via
`__init_subclass__`) — adding a source is *drop a file in `digest/ingest/`,
subclass `IngestorBase`, give it a `name`*. No central list to edit; it appears
in `digest sources` and joins the pipeline automatically. `digest sources` is a
live catalog of every registered ingestor with a status pulse + 7-day ingest
sparkline + lifetime count. A new LLM backend plugs in with `register_backend`.

The remaining design seams (score-factor composition, triage engine, daily-note
hooks; regime deferred) are planned in
[packages/digest-core/SEAMS_PLAN.md](packages/digest-core/SEAMS_PLAN.md).

## Schedule

Staggered with macro digest to avoid MLX contention:

| Job | When |
|---|---|
| am pipeline | daily 04:00 (3h after macro am) |
| pm pipeline | daily 16:00 (3h after macro pm) |
| weekly | Sat 06:00 (after macro Fri-night batch wraps) |

## Prerequisites

- Python 3.12+
- `uv` (`brew install uv`)
- Ollama running locally with `qwen2.5:14b` pulled
- MLX server (managed by macro digest's `com.dr.mlx.server` launchd job)
- EDGAR user agent string (your email, per SEC policy)
- Optional: Reddit script-type app credentials, COURTLISTENER_TOKEN,
  ANTHROPIC_API_KEY / GEMINI_API_KEY for fallback summarizer backends,
  Databricks workspace credentials if enabling the medallion sink

## Getting started

```bash
cd ~/Projects/pc-insurance-digest
uv sync
cp .env.example .env       # fill in EDGAR_USER_AGENT, OBSIDIAN_VAULT_PATH,
                           # REDDIT_* and any optional keys
uv run digest init-db
uv run digest ingest all
uv run digest sources     # live catalog: every source + 7-day ingest pulse
uv run digest brief       # regime + top signals + alert watchlist (offline)
uv run digest stats
uv run digest pipeline --run-type manual
```

CLI commands: `ingest`, `sources`, `brief`, `rate`, `calibration`, `embed`,
`related`, `ask`, `outcomes`, `learn`, `reserving`, `cat-nowcast`,
`severity-tape`, `litigation`, `burden`, `triage`, `summarize`, `regime`,
`signals`, `pipeline`, `publish`, `weekly`, `stats`, `recent`, `health`, `viz`,
`init-db`.

**Scoring feedback loop.** `digest rate <id> <1-5>` records what you thought an
item was worth; `digest calibration` shows system-vs-you deltas; `digest
outcomes` backtests whether ranked items actually mattered (follow-on coverage,
same-insurer EDGAR filing, regime shift, your rating, or a ≥1σ insurer stock
move at 7d/30d). These populate `gold.score_calibration` / `gold.outcome_hit_rate`,
and `digest learn` trains a learned relevance scorer on those labels — reporting
a holdout A/B (top-N precision: heuristic vs learned) and writing a
`learned_score` alongside the heuristic (which stays authoritative). The learned
model is a lean numpy logistic regression (no sklearn/MLflow required on Free
Edition; MLflow logging is used if installed).

**Semantic layer (optional, local).** `digest embed` builds per-item embeddings
via the local Ollama server (`ollama pull nomic-embed-text`); then `digest
related <id>` finds more-like-this and `digest ask "<question>"` answers from
your own corpus (RAG) with citations. Vectors cache in SQLite and mirror to
`pc_bronze.item_embeddings`.

**Lakehouse (Databricks, optional).** With `DATABRICKS_ENABLED=true`, the
pipeline best-effort-mirrors into a shared `digest` catalog (`pc_*` schemas;
macro-ai-digest uses `macro_*`) — DDL in
[packages/digest-core/sql/databricks/](packages/digest-core/sql/databricks/)
(`{bronze,silver,gold}.sql`, `xdomain.sql`, `alerts.sql`). `digest brief` and
`digest calibration` are the offline local equivalents of the gold leaderboard /
calibration views, so analytics work with no warehouse.

## Tests

```bash
uv run pytest        # hermetic — no MLX / Ollama / network / vault required
```

## Scheduling

```bash
bash scripts/install_launchd.sh
launchctl list | grep com.dr.pcdigest
```

## User-editable feedback in `_meta/`

These files in the Obsidian vault drive parts of the pipeline directly:

- **`_meta/Scoring Weights.md`** — YAML frontmatter overrides every leaderboard
  boost factor (sources, topics, insurer priority, keyword boosts, burden
  intensity). `signals.py` re-reads on mtime change.
- **`_meta/Score higher.md`** — items the user wants ranked higher; informs
  prompt tuning + Wave 4 manual-ratings feedback loop.
- **`_meta/Updates.md`** — observation log for items to downvote / drop /
  filter. Each entry tracks `ingested` + `fix applied` checkboxes; closed
  entries describe the code change that landed.

## Project layout

```
pc-insurance-digest/
├── pyproject.toml
├── README.md
├── CLAUDE.md                          # deep design context
├── config/
│   ├── edgar_tickers.yaml             # 15-insurer universe (sync with triage.py)
│   ├── rss_feeds.yaml                 # trade press + Google News proxies
│   ├── subreddits.yaml
│   ├── substack_feeds.yaml
│   ├── fred_series.yaml               # P&C cost-driver CPI/PPI series
│   ├── courtlistener_courts.yaml      # tier-1 / emerging / tier-3 jurisdictions
│   ├── state_doi_sources.yaml         # CA/FL/TX/NY/LA press scrapers
│   ├── serff_states.yaml              # SERFF rate filings + portal dispatch
│   ├── industry_research_sources.yaml # LexisNexis Risk, JD Power
│   ├── investor_supplements.yaml      # per-insurer 10-Q supplement URLs
│   └── naic_schedp_sources.yaml       # reserve-triangle data source
├── launchd/                           # am / pm / weekly plists
├── packages/digest-core/              # shared framework core (PC + macro plug in)
│   ├── EXTRACTION_PLAN.md             # what-moves-where map
│   ├── SEAMS_PLAN.md                  # Phase 2: design seams + macro port
│   ├── sql/databricks/{bronze,silver,gold}.sql
│   └── src/digest_core/               # types · db · ingest · summarize · obsidian · cli · sinks
├── tests/                             # hermetic pytest suite
├── scripts/install_launchd.sh
└── src/digest/
    ├── cli.py                         # Click entry points
    ├── config.py
    ├── db.py                          # SQLite schema + auto-keep hooks
    ├── triage.py                      # Ollama prompt + 17-topic taxonomy
    ├── summarize.py                   # P&C prompt + caps (backends/runner in digest_core)
    ├── regime.py                      # market_cycle × cat_load detector
    ├── signals.py                     # 11-factor leaderboard + conviction tier
    ├── obsidian.py                    # daily / weekly / topic-archive writer (primitives in digest_core)
    ├── weekly.py                      # weekly synthesis (themes / must-reads)
    ├── health.py
    ├── security.py
    ├── viz.py
    ├── sinks/                         # shim → digest_core.sinks.databricks
    │   ├── __init__.py
    │   └── databricks.py              # medallion sink, no-op by default
    └── ingest/                        # rss/substack/hn/reddit/edgar delegate to digest_core
        ├── base.py                    # binds digest_core IngestorBase → db (store)
        ├── edgar.py, rss.py, reddit.py, substack.py, hackernews.py
        ├── nhc.py, usgs.py, spc.py, nifc.py
        ├── fred.py
        ├── courtlistener.py
        ├── state_doi.py, serff.py
        ├── industry_research.py, collision_data.py
        ├── investor_supp.py, naic_schedp.py
```

## Wave 4 roadmap

- **Regulatory Sonar full detector** — periodic per-state burden-pressure
  index, LegiScan integration, weekly note section
- **Score Higher / Updates feedback automation** — parse the `_meta/` notes
  into `silver.manual_ratings`, auto-suggest boost adjustments
- **Databricks dashboards + Genie space** — daily / weekly leaderboards,
  source quality, boost-factor heat-map
- **Cross-feed dedup** — title-normalize hash pass at triage entry
- **digest-core Phase 2** — the code-moving extraction is done (shared mechanics
  now live in `digest_core`); next is the design seams (regime axes, score-factor
  composition, triage engine) + porting macro-ai-digest onto the core. See
  [SEAMS_PLAN.md](packages/digest-core/SEAMS_PLAN.md)

See [_meta/To-Do.md](https://github.com/dram-dev/pc-insurance-digest) (in the
Obsidian vault) for the full backlog.
