# P&C Insurance & Financial Services Digest

Daily + weekly curated digest covering US P&C insurance and financial services.
Sibling to [macro-ai-digest](../macro-ai-digest); shares MLX/Ollama backends and
the same Obsidian vault (lands in `81 P&C Digest` next to `80 Digest`).

For the full design context (locked decisions, scoring formula, regime axes,
Regulatory Sonar, etc.), see [CLAUDE.md](CLAUDE.md).

## Status (Waves 1–4 shipped · all sources live · digest-core foundation extracted · local Analyst MCP agent · scoring-math wave 2026-06)

**Pipeline:** `ingest → triage (Ollama Qwen2.5:14b) → summarize (MLX Qwen3.5-27B
local) → score (signals leaderboard) → publish (Obsidian)`. Every stage's LLM is
config-swappable through a backend registry — `digest models` prints the
backend/model/endpoint per stage and pings each for reachability.

**Ingestors (live):**
- **EDGAR** — 15-insurer universe (TRV, ALL, PGR, CB, HIG, AIG, MET, PRU, RNR,
  EG, AXS, MMC, AON, WTW, BRK); per-form selection so the annual 10-K is never
  buried behind monthly 8-Ks / Form 4s; age-aware body fetch (a fresh 10-K gets
  its content even months late, fetched once) with MD&A combined-ratio /
  reserve-development extraction; Python auto-keep
- **Trade press** — Insurance Journal, Reinsurance News, Artemis, Carrier
  Management + Google News `site:` proxies for FT / Economist / WSJ /
  Bloomberg insurance desks
- **Cat events** — NHC tropical cyclone (U.S./Caribbean threat filter), USGS
  M≥5.0 earthquakes (M≥6.0 U.S./territory auto-keep), SPC severe-weather
  outlooks, NIFC active wildfires ≥1000 ac
- **State DOI** — direct press-release scrapers for all 5 priority states
  (CA/FL/TX/NY/LA); the JS-rendered / WAF-blocked ones (TX year index, LA LDI)
  go through the headless-browser render path
- **SERFF rate filings** — all 5 states, three portal types: TX/NY/LA via the
  standard SERFF Filing Access portal (headless PrimeFaces search flow), **CA**
  via CDI's YTD approvals **Excel** (carries the requested/approved rate %, so it
  honours the ≥5% threshold), **FL** via FLOIR's IRFS **JSON API** (scoped to the
  personal-auto big-3: Progressive / GEICO / Liberty Mutual)
- **Collision claims** — CCC *Crash Course* + Mitchell/Enlyte industry-trend
  reports (severity / repair-cost / parts signal → `supply_chain`)
- **Industry research** — LexisNexis Risk Solutions + JD Power (render path;
  WAF-bypassed)
- **FRED** — 7 P&C cost-driver CPI/PPI series with ±1.5σ anomaly gate
- **CourtListener** — federal MDL docket tracker (tier-1 + emerging
  jurisdictions, P&C NOS filter, MDL keyword auto-keep)
- **LegiScan** — state insurance-bill velocity, feeding the per-state burden
  barometer (`digest burden`)
- **Reinsurance pricing (EKG Lead 1)** — Guy Carpenter U.S. Property-Cat
  Rate-on-Line index (via Artemis), nudging the regime market-cycle axis
- **Investor supplements** — per-insurer 10-K ASC-944 loss-development triangles
  (PGR live → chain-ladder reserving quant)
- **Reddit / Substack / Hacker News** — r/Insurance, r/Actuary, r/CFP,
  weather/EQ subreddits (Reddit via the public `.rss` feed — no API key);
  Insurance Insider, Coverager; HN ≥100 points
- **Still scaffolded:** NAIC Schedule P reserve triangles

**Headless-browser render** (optional `render` extra): JS-rendered or WAF-blocked
sources — the SERFF standard portal, state DOI TX·LA, LexisNexis, JD Power — are
fetched with Playwright (`digest/ingest/render.py`: a lazy, graceful
`fetch_rendered` / `fetch_rendered_interactive` / `fetch_rendered_paginated`).
One-time setup: `uv sync --extra render && uv run playwright install chromium`.
When it's absent, those sources skip cleanly; everything else is plain `requests`.

**Insurance EKG leads** (quantitative vital-signs that harden the regime detector
and the leaderboard, rather than new architecture): reinsurance ROL · CAT-load
nowcast (`digest cat-nowcast` — a per-calendar-month Poisson/negative-binomial
count model over ~10y of OpenFEMA declarations; escalates on the seasonal tail
probability, p<0.05 → active_season, p<0.005 → post_major_event) · severity tape
(`severity-tape` — a **loss-cost-weighted** blend of the FRED cost drivers:
parts .30 / labor .30 / medical .20 / used-vehicle .10 / property .10, with an
optional median/MAD robust z) · litigation / TPLF docket pressure (`litigation`)
· capital flows · chain-ladder reserving (`reserving`) · disclosure sentiment
(`disclosure`) · per-state burden barometer (`burden`). Each wires into an
existing scoring factor and is behaviour-preserving until its data flows.

**Triage / summarize / score:**
- 17-topic P&C taxonomy + `litigation_tplf` sub-tag (canonical list in
  [src/digest/triage.py](src/digest/triage.py))
- Hybrid auto-keep — Python enforces material categories (insurer 8-K/10-Q/10-K,
  NHC advisories, U.S. M≥6.0 quakes, FRED anomalies, CourtListener MDLs, state
  DOI bulletins, SERFF rate filings, LegiScan bills, investor supplements, NAIC
  Schedule P); model handles the rest
- Two-axis regime detector — `market_cycle × cat_load`, 72h cadence with
  override file. The market cycle is a **Markov-switching hidden state**: a
  5-state forward filter with a sticky transition prior (self ≈ 0.90) treats
  the LLM classification as a noisy emission (0.70 diagonal confusion kernel)
  and the priced reinsurance hint as a second emission; the regime multiplier
  is the posterior-expected value (a continuous glide, not a ±10-point cliff),
  with the full posterior persisted in `evidence_json`
- Signal leaderboard — 12-factor score `source × regime × topic_relevance ×
  recency × llm_judgment × topic_priority × burden_intensity × insurer_priority
  × inflation_keyword × regulatory_action × litigation_tplf ×
  reserve_deterioration`. Recency is exponential decay with **per-topic
  half-lives** (cat_event 2d · regulatory_rate 14d · reserving 21d · default
  7d); the three keyword families are disjoint with a 1.6 stack cap (no phrase
  double-counts); `llm_judgment` swaps to an isotonic-calibrated relativity
  `P(corroborated)/base_rate` once `digest learn` has ≥100 labeled outcomes.
  All boost values are user-editable from the Obsidian vault — see
  _meta/Scoring Weights.md_. Two learned layers ride alongside, gated and
  advisory: a `learned_score` (numpy logistic regression) per row, and a
  **log-linear exponent gate** (`log S = Σ w·log f`, ridge-shrunk toward the
  hand-set w=1) that becomes eligible only after two consecutive weekly
  out-of-sample wins and applies only with `loglinear: {apply: 1}`; the
  heuristic stays authoritative.
- Conviction tier — each scored item is tagged 🔴 high / 🟡 medium / 🔵 low.
  Cutoffs are **quantile-calibrated** (trailing-90d P90/P60 of latest scores,
  once ≥80 exist; quantiles tunable in _meta/Scoring Weights.md →
  `signal_tiers`, with the fixed values as fallback) and shown as a badge on
  the daily + weekly leaderboards; the persisted `signal_scores.tier` is
  authoritative (and mirrors to the Databricks silver layer)
- Source credibility — Bühlmann–Straub experience rating of the hand-set
  source multipliers from observed corroboration (`Z = n/(n+k)`,
  `k = EPV/VHM`); report-only in the weekly note + `digest credibility` until
  `credibility: {apply: 1}` swaps the implied multipliers into scoring
- Regulatory Sonar **lite** — `burden_direction` / `burden_intensity` on
  `regulatory_rate` items, with leaderboard boost and a daily-note callout on
  high-intensity items

**Publish:** Daily + weekly notes + per-topic archives in
`{vault}/81 P&C Digest/{Daily,Topics,Weekly}/`, plus a `_meta/` folder for
operations log, scoring weights, and feedback files. Each daily note opens with a
**Market EKG** vital-signs panel (regime quadrant · gauges · severity drivers ·
litigation velocity · burden bars · reserve heat-grid). `digest dashboard`
generates two standalone cockpit notes — **Signal Desk** (live DataviewJS
regime/vitals timeline + reserve Sankey + cat-season heatmap + calibration
heatmap) and **Home** (status strip + pipeline buttons) — refreshed automatically
by the weekly job. `digest viz --lab` renders a side-by-side eval harness of the
candidate Markdown-viz techniques.

**Optional Databricks medallion sink** — bronze / silver / gold DDL ships in
`packages/digest-core/sql/databricks/`. `DatabricksSink` (implemented in
`digest_core.sinks.databricks`, wired through `src/digest/sinks/`) is
best-effort + lazy-connected and no-ops unless `DATABRICKS_ENABLED=true`;
SQLite remains source of truth.

## Analyst — local data-analyst agent (MCP)

A read-only analytics agent over the warehouse, for asking *why* about the
ingested data — root-cause a ranking, decompose a reserving signal, explain a
feed's behaviour — with the rigor of a P&C actuary. Three pieces:

- **Agent Server** ([src/digest/mcp_server.py](src/digest/mcp_server.py)) — a
  FastMCP stdio server opened **read-only** (`mode=ro`), so no tool — including
  arbitrary `run_sql` — can mutate the source of truth. Tools: `run_sql`,
  `list_tables`, `describe_table`, `data_overview`, `score_breakdown` (decomposes
  the 12-factor leaderboard for one item), `pipeline_health`, `source_quality`,
  `top_signals`; plus `schema://overview` / `formula://scoring` resources and an
  `analyst` prompt. Claude Code auto-discovers it via [.mcp.json](.mcp.json); run
  standalone with `uv run --extra mcp digest-mcp`.
- **Analyst subagent** ([.claude/agents/analyst.md](.claude/agents/analyst.md)) —
  a senior P&C **actuary + data scientist** persona scoped to the Agent Server
  tools and the method skills below.
- **Method skills** ([.claude/skills/](.claude/skills/)) — modular, on-demand
  actuarial techniques, each a `SKILL.md` + `reference.md` + a stdlib-only,
  hand-calc-verified helper script:

  | Skill | Does |
  |---|---|
  | `reserving-chain-ladder` | loss triangles → LDF/CDF → per-AY ultimate & IBNR, adverse vs favorable development (reconciles to `reserving_signals`) |
  | `bornhuetter-ferguson` | BF + Cape Cod reserving — blend an a-priori expected loss with development for green/low-credibility AYs; shows BF vs chain-ladder |
  | `combined-ratio-bridge` | decompose a combined ratio into loss/LAE/expense, strip cats + prior-year development → **underlying** margin; GAAP vs statutory; period-over-period bridge |
  | `ratemaking-indication` | indicated rate change — loss-ratio & pure-premium methods |
  | `credibility-weighting` | classical + Bühlmann / empirical-Bayes credibility |
  | `glm-pricing` | Poisson / Gamma / Tweedie IRLS → multiplicative rating relativities |
  | `severity-trend-decomposition` | log-linear loss-cost trend + frequency × severity split |

Enable with the `mcp` extra (`uv sync --extra mcp`). The agent reads the live
SQLite warehouse, so analytics work with no MLX / Databricks.

## Insurer fundamentals (10-K XBRL + statutory)

A structured-financials layer over the P&C universe, from one XBRL instance fetch
per filer plus statutory feeds:

- **`digest ingest-xbrl`** — a *concept registry*
  ([src/digest/parse/xbrl_facts.py](src/digest/parse/xbrl_facts.py)) pulls every
  standardized disclosure out of each insurer's 10-K XBRL instance — premiums,
  claim counts (frequency), IBNR, reserve development, reinsurance, investments,
  DAC, segment results, combined-ratio components, and the ASC 944 loss-development
  **triangles** — by segment / product / accident-year / geography, into
  `insurer_xbrl_facts` (+ `loss_triangles`, feeding `digest reserving`).
- **`digest ingest-statutory`** — free III top-writer tables → `statutory_facts`
  for the big mutuals absent from SEC XBRL (State Farm, USAA, Liberty Mutual, …).
- **`digest canonicalize`** — unifies the source-specific LOB strings onto one
  `canonical_lob` taxonomy so a line compares across insurers.
- **`digest underwriting`** — loss&LAE / expense / combined ratios per insurer,
  with the **earned-premium denominator validated against net written premium**
  (`EP/WP ≈ 1.0 ✓`). The combined ratio is shown only where the EP validates *and*
  the full expense is tagged (never backed out of a GAAP operating-profit line).
- **`digest ingest investor_supp`** — additionally parses the carrier-**reported**
  combined ratio from the investor-supplement highlights table and keeps only the
  candidate that **reconciles with the independent XBRL figure** (PGR: computed
  87.4% vs reported 87.4%) — never a guessed value. Reported figures land in
  `statutory_facts` and surface in `digest underwriting` (✓ = the two agree).
- **`digest pure-premium`** — **frequency × severity × pure-premium** decomposition
  per accident year from those same facts: severity = incurred/claims per product
  line, frequency = claims per $M earned premium per segment (EP is the exposure
  *proxy*, so the trend is net of rate), pure premium = incurred/EP — reconciling
  exactly to freq × sev. Log-linear trends fit over **mature** accident years only
  (the latest AY is still developing), with a YoY fallback while the EP history is
  short. The annualized **loss-cost trend** `(1+freq)(1+sev)−1` is the rate-need
  proxy to hold against a carrier's own SERFF ask, and the carrier severity trends
  cross-check against the FRED severity tape (`digest severity-tape`). Rows land in
  `freq_sev_signals`; the `pure_premium(ticker)` MCP tool serves the same read.

The cross-insurer figures and the `fundamentals(ticker)` MCP tool give the Analyst
a structured read of each carrier's underwriting results alongside the method skills.

## Alpha engine — return forecasts (local ML)

A **fully local** ML layer that predicts each public insurer's **forward,
benchmark-relative (vs IAK) stock return** from the digest's own data + signal
scores. An early-warning / alpha signal that rides *alongside* the heuristic
leaderboard and **never feeds it**. Opt-in deps: `uv sync --extra ml`
(scikit-learn `HistGradientBoosting` is the primary learner — no `libomp` needed;
LightGBM is an optional accelerator; pandas builds the panel). Optional Apple
**MLX** neural head (`--extra ml-mlx`, Mac-only) and **shap** (`--extra ml-explain`).

- **`digest forecast prices`** — daily close store for the modeled insurer
  universe + IAK/SPY benchmarks ([src/digest/prices.py](src/digest/prices.py)),
  via **Tiingo**
  (set `TIINGO_API_TOKEN` — the reliable source; Yahoo 429s and Stooq challenges
  unauthenticated traffic), with a Yahoo cookie+crumb session + backoff fallback.
- **`digest forecast backtest`** — the honest scorecard: a **purged + embargoed
  walk-forward** reporting out-of-sample **IC**, hit-rate and a top-minus-bottom
  **long-short** return **vs three baselines** (zero, momentum, signal-only). It
  flags "no edge — treat as noise" unless IC is *positive* and beats the baseline.
- **`digest forecast train` / `predict`** — fit + persist a model
  ([src/digest/alpha.py](src/digest/alpha.py)) and write per-insurer forecasts.
  Surfaces in the Obsidian Signal Desk "📈 Signal → Return Watch" panel and the
  `return_forecasts` MCP tool.

The leakage-safe (insurer, as-of) feature panel lives in
[src/digest/features.py](src/digest/features.py). **Predictive power depends on
per-insurer signal density** — until scored items densely map to the tickers, the
model has little beyond price momentum and will (correctly) report no edge.

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
- Optional: the `render` extra + Playwright Chromium (`uv sync --extra render &&
  uv run playwright install chromium`) for JS/WAF-blocked sources;
  COURTLISTENER_TOKEN and LEGISCAN_API_KEY for those ingestors; ANTHROPIC_API_KEY
  / GEMINI_API_KEY for fallback summarizer backends; the `mcp` extra
  (`uv sync --extra mcp`) for the local Analyst agent; the `ml` extra
  (`uv sync --extra ml`) + `TIINGO_API_TOKEN` for the alpha engine's return
  forecasts; Databricks workspace credentials if enabling the medallion sink
  (Reddit needs no key — it uses the public `.rss` feed)

## Getting started

```bash
cd ~/Projects/pc-insurance-digest
uv sync
# JS-rendered / WAF-blocked sources (SERFF standard portal, state DOI TX·LA,
# LexisNexis, JD Power) need the headless browser — opt in once:
uv sync --extra render && uv run playwright install chromium
cp .env.example .env       # fill in EDGAR_USER_AGENT, OBSIDIAN_VAULT_PATH,
                           # REDDIT_* and any optional keys
uv run digest init-db
uv run digest ingest all
uv run digest sources     # live catalog: every source + 7-day ingest pulse
uv run digest models      # backend/model/endpoint per stage + reachability
uv run digest brief       # regime + top signals + alert watchlist (offline)
uv run digest stats
uv run digest pipeline --run-type manual
```

CLI commands: `ingest`, `sources`, `models`, `brief`, `rate`, `calibration`,
`credibility`, `embed`, `related`, `ask`, `outcomes`, `learn`, `forecast`,
`reserving`, `disclosure`, `cat-nowcast`, `severity-tape`, `litigation`,
`burden`, `triage`, `summarize`, `regime`, `signals`, `pipeline`, `publish`,
`weekly`, `stats`, `recent`, `health`, `viz`, `dashboard`, `init-db`.

**Scoring feedback loop.** `digest rate <id> <1-5>` records what you thought an
item was worth; `digest calibration` shows system-vs-you deltas; `digest
outcomes` backtests whether ranked items actually mattered at 7d/30d —
follow-on coverage elevated vs the cohort, a same-insurer EDGAR filing, your
rating, or a **benchmark-excess stock move** (vs the stored IAK/SPY closes)
that survives **Benjamini–Hochberg FDR control (q=0.10)** across the run's
cohort, never a fixed-σ trigger. These populate `gold.score_calibration` /
`gold.outcome_hit_rate`, and the weekly `digest learn` trains on those labels
with a **chronological + embargoed** holdout (bootstrap CIs on AUC and
precision@k) — writing a `learned_score` alongside the heuristic, fitting the
isotonic materiality calibrator, and evaluating the log-linear exponent gate.
The heuristic stays authoritative throughout; the learned model is a lean
numpy logistic regression (no sklearn/MLflow required on Free Edition; MLflow
logging is used if installed).

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
  boost factor (sources, topics, insurer priority, keyword boosts incl.
  `stack_cap`, burden intensity), plus tier quantiles (`signal_tiers`),
  per-topic recency half-lives (`recency_half_lives`), and the two activation
  flags (`credibility: {apply: 1}`, `loglinear: {apply: 1}`). `signals.py`
  re-reads on mtime change.
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
│   ├── state_doi_sources.yaml         # CA/FL/TX/NY/LA press scrapers (live)
│   ├── serff_states.yaml              # SERFF rate filings — 5 states, 3 portals
│   ├── reinsurance_sources.yaml       # Artemis ROL index (EKG Lead 1)
│   ├── industry_research_sources.yaml # LexisNexis Risk, JD Power (render)
│   ├── investor_supplements.yaml      # per-insurer 10-K triangle URLs (PGR live)
│   └── naic_schedp_sources.yaml       # reserve-triangle data source (scaffold)
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
    ├── regime.py                      # market_cycle (Markov-switching filter) × cat_load
    ├── reinsurance.py                 # EKG Lead 1 — ROL pricing → market_cycle emission
    ├── signals.py                     # 12-factor leaderboard + quantile conviction tiers
    ├── calibration.py                 # isotonic (PAVA) materiality → P(corroborated)
    ├── credibility.py                 # Bühlmann–Straub source credibility (report-only)
    ├── loglinear.py                   # learned score exponents, gated + opt-in
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
        ├── render.py                  # Playwright headless fetch (JS/WAF sources)
        ├── edgar.py, rss.py, reddit.py, substack.py, hackernews.py
        ├── nhc.py, usgs.py, spc.py, nifc.py
        ├── fred.py
        ├── courtlistener.py, legiscan.py
        ├── state_doi.py, serff.py     # SERFF: TX/NY/LA portal · CA xlsx · FL API
        ├── industry_research.py, collision_data.py
        ├── investor_supp.py, naic_schedp.py
```

## Roadmap

Wave 4's insurance-EKG leads, the Obsidian viz/dashboard surface, the LLM
plug-and-play registry, and all source build-out (collision + all 5 SERFF states)
have shipped. Still open:

- **Regulatory Sonar full detector** — the LegiScan ingestor is live; the
  periodic per-state burden-pressure detector + weekly note section is pending
- **SERFF / FL detail enrichment** — the SERFF standard portal and the FL API
  list views omit the rate-% / LOB / closed-date (those live on each filing's
  detail page); a per-filing detail fetch would add them
- **NAIC Schedule P** — the last scaffolded source (reserve triangles)
- **Score Higher / Updates feedback automation** — parse the `_meta/` notes
  into `silver.manual_ratings`, auto-suggest boost adjustments
- **Databricks Genie + AI/BI dashboards** — the sink + medallion DDL are live;
  the Genie space and warehouse dashboards over the gold views are user-side
- **Cross-feed dedup** — swap triage's title-fuzz dedup for the semantic
  `near_duplicates()` pass
- **Scoring-math wave 2** (the 2026-06-09 wave shipped labels/FDR, isotonic
  calibration, quantile tiers, Bühlmann credibility, the log-linear gate, the
  Markov-switching regime, the seasonal cat nowcast, and the weighted severity
  tape) — remaining candidates: **Mack-σ-scaled reserve boost** (boost on the
  z of adverse development, not the point move), **conformal triage band**
  (uncertain keep/drop → kept-with-flag), **corroboration-calibrated recency
  half-lives** (fit decay curves from outcome-vs-age once the label store
  matures), alpha-engine IC bootstrap CIs
- **digest-core seams** — the foundation is extracted and macro-ai-digest runs on
  the same core; the remaining design seams (score-factor registry, triage
  engine, daily-note hooks; regime deferred) are reactive. See
  [SEAMS_PLAN.md](packages/digest-core/SEAMS_PLAN.md)

See [_meta/To-Do.md](https://github.com/dram-dev/pc-insurance-digest) (in the
Obsidian vault) for the full backlog.
