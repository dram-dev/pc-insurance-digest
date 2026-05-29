# digest-core — Phase 2 Plan: the design seams

The **code-moving** extraction is done (see EXTRACTION_PLAN.md §1). Everything
domain-agnostic now lives in `digest_core`; PC Digest consumes it via thin
shells/aliases and is green on 65 tests, all on `master`.

What remains is the genuinely *new* design work — the EXTRACTION_PLAN.md §3
"tricky seams." This file is the concrete pick-up plan for that.

## Status snapshot (what's already in core)

| Module | Lifted | PC shape |
|---|---|---|
| `types.IngestedItem` | ✅ | re-export |
| `db.{schema,helpers}` | ✅ | thin wrappers default db_path + sink fan-out |
| `ingest.base` (IngestorBase + ItemStore) | ✅ | `class IngestorBase(CoreBase): store = db` |
| `ingest.{rss,hackernews,reddit,edgar}` | ✅ | shells hold config, delegate fetch() |
| `summarize.{backends,runner}` | ✅ | `_backend_config()`; cap values stay domain |
| `obsidian.{paths,render,archive}` | ✅ | `Paths` subclass `resolve()`; topic maps stay |
| `cli.base` (load_ingestor, run_ingest) | ✅ | group + commands + INGESTORS stay |
| `sinks.databricks` | ✅ | singleton built from PC settings |

Tests scaffold (EXTRACTION_PLAN.md §4 open-q #5): ✅ done — `tests/`, hermetic.

## The guiding decision

Per EXTRACTION_PLAN.md §5 step 4 and the project rule *"don't pre-design the
framework — wait until divergence is visible,"* the seams below should be
**driven by porting macro-ai-digest onto core**, not designed from PC alone.
macro is the second concrete data point that reveals each seam's real shape.

**Recommended Phase-2 order:**
1. **Stand up the macro port skeleton** — point macro-ai-digest at `digest-core`
   (workspace dep), lift its `IngestedItem`/`db`/`ingest.base`/backends the same
   way PC was (these are already proven generic; should be near-mechanical).
   This surfaces which "seams" are actually shared vs. truly divergent.
2. **Then tackle seams in this order** (most-self-contained first):
   regime axes → signals factors → triage engine → daily-note hooks →
   topic-cap/materiality → CLI group-factory.

If macro isn't ready, the most self-contained seam to prototype against PC
alone is **regime axes** — but design it with macro's single-axis regime in
view (notes below).

## Per-seam design sketches

### 1. Regime — N-axis abstraction  (EXTRACTION_PLAN §3.1)
- **STATUS (2026-05-28): DEFERRED after recon.** Read macro's `macro_regime.py`
  against PC's `regime.py`: they are NOT peers. macro = 1 mechanical axis,
  ISO-week-keyed, upsert-by-week, **no** hysteresis/override/staleness/LLM. PC =
  2 axes (1 LLM-judged + 1 mechanical), timestamp-keyed append-history, **with**
  hysteresis + override + staleness. The only shared concept is "classification →
  multiplier + prompt framing." A `RegimeAxis`/`RegimeDetectorBase` now would be
  either too thin to matter or an over-parameterized mess — exactly the "simpler
  base may be right / don't pre-design" risk this section flagged. **Revisit only
  during/after the macro port**, and consider that regime may legitimately stay
  domain-specific in both.
- **Divergence:** PC = 2 axes (`market_cycle` LLM-judged × `cat_load` mechanical);
  macro = 1 axis (`macro_regime`, weekly).
- **Proposed:** core `RegimeAxis` (name, states→multiplier, `compute()->state`) +
  `RegimeDetectorBase` owning hysteresis + override + staleness + the
  `regime_signals` table schema. PC registers `MarketCycleAxis`+`CatLoadAxis`;
  macro registers one axis.
- **Decide at port:** is macro's regime really a peer axis, or one-state-per-week
  (which might want a simpler base than `RegimeAxis`)? Confirm before locking.
- **Watch:** PC's `compute_market_cycle` now calls the backend with the
  3-arg signature + its own `MARKET_CYCLE_SYSTEM_PROMPT` — keep that when
  generalizing the LLM-judged-axis path.

### 2. Signals — score-factor composition  (EXTRACTION_PLAN §3.2)
- **Divergence:** shared `source × recency × llm_judgment`; PC adds
  topic_priority/burden/insurer/inflation/regulatory/tplf + the conviction
  `tier`; macro will want its own (signal_outcomes z-score, cluster size…).
- **Proposed:** core registry of `ScoreFactor(name, fn(item, regime)->float)`;
  core multiplies registered factors; core ships the common ones
  (recency, llm_judgment, source-mult lookup against a domain table) + the
  user-tunable-weights mechanism (already generic-ish in PC's `_load_scoring_weights`).
- **Decide at port:** if macro is "same formula, different numbers," a
  config-driven table beats a plugin system — don't build the registry until
  macro proves it needs custom factors.
- **Carry:** the conviction `tier` (`tier_for_score`/`tier_badge`) + its
  `Scoring Weights.md` `signal_tiers` thresholds are PC-shipped; decide if
  tiering is core mechanism (likely yes) with domain thresholds.

### 3. Triage — engine + auto-keep hooks  (EXTRACTION_PLAN §3.3)
- **Generic:** the flow (Python preprocessor → Ollama → JSON parse → DB update).
- **Domain:** prompt, 17-topic enum, auto-keep rules, `burden_*` fields.
- **Proposed:** core `TriageEngine` with hooks: `domain_system_prompt: str`,
  `auto_keep_steps: list[Callable[[], int]]` (run before LLM),
  `verdict_normalizer: Callable[[dict], dict]`. Core enforces decision/score/topic
  shape only.
- **DONE (2026-05-28):** unified the JSON extractor. `digest_core.summarize.
  runner.extract_json` now uses the brace-depth scan (first balanced object —
  robust to nested braces + trailing prose); triage dropped its duplicate and
  imports the core one, so triage + summarize + regime share a single, more
  robust extractor. Tested. (The rest of the TriageEngine seam still waits for
  the macro port.)

### 4. Obsidian — daily/weekly note extension hooks  (EXTRACTION_PLAN §3.4)
- **Generic:** note shell, topic grouping, weekly synthesis sections, the
  already-lifted render primitives + Paths + index block.
- **Proposed:** core `DailyNoteBuilder` with extension points for "extra
  top-of-note callouts" (PC adds regime + sonar) + domain-provided
  `topic_label/callout/emoji/order` maps. Decide whether the Top-Signals
  leaderboard section is core (feels core, but per-source quality table assumes
  domain source weights).

### 5. Per-topic caps + materiality anchoring  (EXTRACTION_PLAN §3.5)
- Mechanics are generic (cap = `enforce_topic_caps`, already in core; materiality
  clamp). *Which* topic is capped + the materiality anchor text are domain.
- **Proposed:** core summarize prompt template interpolates a
  `domain_materiality_anchor: str`; `TOPIC_CAP_PCT` passed in. Verify macro even
  wants caps before abstracting further.

### 6. CLI — group factory  (deferred from cli.base)
- Core `build_cli(name, help, ingestors, db, console, log_level)` returning a
  Click group with generic commands (ingest/stats/recent/init-db) registered +
  db/console injected; domain adds its own commands. Needs the db-module DI
  decision — settle alongside the §4 "config.py settings subclassing" question.

## Open questions to settle (EXTRACTION_PLAN §4)
- Monorepo vs. separate repo for `digest-core` (currently `packages/`).
- Pin core's Python floor (PC ≥3.12, core declares ≥3.11 — reconcile).
- **DB schema ownership / migration sequencing**: core owns items/run_log/
  summarizer_log; each domain layers its own migrations (PC already does via
  `core_db.init_db_with_migrations(path, MIGRATIONS)`). Confirm macro fits.
- pydantic-settings `BaseSettings` subclassing across packages (for the CLI/DI seam).

## Definition of done for Phase 2
- macro-ai-digest runs end-to-end on `digest-core`.
- Each seam has a core abstraction + both domains as plug-ins, with tests.
- No domain still duplicates code that another domain also has.

## Loose ends carried (not blockers)
- **Mac mini, one-time:** `ALTER TABLE silver.signal_scores ADD COLUMN tier STRING;`
  (conviction tier sink write is best-effort until then).
- **Config bug:** `hackernews.QUERIES` are still macro AI/semis terms — retune to
  P&C insurtech/cyber/cat.
- **Dead branch:** edgar `is_fund → 'fed_markets'` (macro residue; never fires).
- **Pre-existing lint:** ~23 ruff style nits in viz/summarize-stubs/collision/
  health/serff (not bugs/security) — sweep opportunistically.
