# digest-core — Phase 2 Plan: the design seams

The **code-moving** extraction is done (see EXTRACTION_PLAN.md §1). Everything
domain-agnostic now lives in `digest_core`; PC Digest consumes it via thin
shells/aliases and is green on 65 tests, all on `master`.

What remains is the genuinely *new* design work — the EXTRACTION_PLAN.md §3
"tricky seams." This file is the concrete pick-up plan for that.

## Status snapshot (what's already in core)

| Module | Lifted | PC shape | macro shape |
|---|---|---|---|
| `types.IngestedItem` | ✅ | re-export | re-export (via `ingest.base`) |
| `db.{schema,helpers}` | ✅ | thin wrappers + sink fan-out | thin wrappers (no sink) + macro MIGRATIONS |
| `ingest.base` (IngestorBase + ItemStore) | ✅ | `class IngestorBase(CoreBase): store = db` | same, `register=False` base |
| `ingest.registry` (`@__init_subclass__` auto-register + `discover`) | ✅ | `discover_ingestors("digest.ingest")` | same |
| `ingest.{rss,hackernews,reddit,edgar}` | ✅ | shells delegate fetch() | full impls extend base (shells optional) |
| `summarize.{backends,runner}` | ✅ | `_backend_config()` (800 tok) | `_backend_config()` (600 tok) |
| `summarize.backends` registry (`register_backend`) | ✅ | — | — |
| `obsidian.{paths,render,archive}` | ✅ | `Paths` subclass; topic maps stay | not yet adopted (macro obsidian.py untouched) |
| `cli.base` (load_ingestor, run_ingest, discover_ingestors) | ✅ | group + commands stay | group + commands stay |
| `catalog` (`digest sources`) | ✅ | `sources` command | `sources` command |
| `sinks.databricks` | ✅ | singleton built from PC settings | n/a (macro has no sink) |

Tests: PC `tests/` hermetic (88). macro `tests/` hermetic (12, new this round).

## Status: macro port DONE (foundation) — 2026-05-28

macro-ai-digest now runs on `digest-core` (editable path dep from the sibling
repo). The proven thin-shell pattern lifted db / ingest.base / summarize
backends+runner / triage extract_json; macro keeps all its domain logic
(macro_regime, essays, debate, velocity, clustering, dashboard, ~15 ingestors).
Net ~295 fewer lines in macro.

**The "grow organically" mechanism shipped:** the ingestor registry. Adding a
source in either domain is now *drop a file, subclass `IngestorBase`, give it a
`name`* — it self-registers (via `__init_subclass__`), appears in `digest
sources`, and is runnable via `digest ingest <name>` / pipeline. No central
INGESTORS dict to edit. New LLM backends plug in via `register_backend` without
touching core. `discover()` is import-isolated so a missing optional dep
degrades to a reported failure, not a crash.

## The guiding decision (still holds for the remaining seams)

Per EXTRACTION_PLAN.md §5 step 4 and *"don't pre-design the framework — wait
until divergence is visible,"* the **remaining** seams should be driven by the
two now-concrete domains side by side. The foundation port proved which
substrate is genuinely shared (it all lifted cleanly). The deeper seams below
each need a deliberate two-domain design pass:

**Recommended next order** (most-self-contained first), now that both domains
are on core:
1. **signals factors** — both have `source × recency × llm_judgment`; PC adds
   6 boost factors + conviction `tier`, macro adds signal_outcomes z-score +
   cluster size. Good first registry candidate (`ScoreFactor`).
2. **triage engine** — flow is identical (Python preprocessor → Ollama → JSON
   parse → DB update); prompt/topics/auto-keep are domain. extract_json already
   shared. `TriageEngine` with hooks.
3. **obsidian daily/weekly hooks** — macro's obsidian.py is NOT yet on core's
   render/paths/archive primitives; adopting them is the next mechanical lift,
   then the `DailyNoteBuilder` extension-point design.
4. **regime** — still DEFERRED (see §1): the two regimes are not peers.
5. **topic-cap/materiality**, **CLI group-factory** — last.

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

## Adding a new source (the organic-growth workflow)

This is the premise the registry exists to serve — in **either** domain:

1. Add `src/digest/ingest/<source>.py`.
2. `class <Name>Ingestor(IngestorBase): name = "<source>"; def fetch(self): ...`
   (optionally `tags = (...)`, `order = N`, and a one-line docstring/module
   docstring for the catalog).
3. That's it. It self-registers → shows in `digest sources` (as `never-run`),
   is runnable via `digest ingest <source>`, and joins the pipeline's stage 1.

No central dict to touch. A new LLM backend: `register_backend("name", fn)` in
the domain (fn takes `(system_prompt, user_prompt, BackendConfig)`). A source
whose module fails to import (missing optional dep) is reported in `digest
sources`, not silently dropped.

## Definition of done for Phase 2
- macro-ai-digest runs end-to-end on `digest-core`. ✅ (foundation; obsidian +
  the deeper seams remain)
- Each seam has a core abstraction + both domains as plug-ins, with tests.
  (ingest/db/backends/runner/catalog ✅; signals/triage/obsidian-hooks pending)
- No domain still duplicates code that another domain also has. (foundation
  de-duped; signals/triage/obsidian still parallel)

## Loose ends carried (not blockers)
- **macro `gmail`** last-run errors (surfaced by `digest sources` — `✗ error`).
  Likely OAuth token refresh; check `secrets/` + scopes on the Mac mini.
- **Mac mini, one-time:** `ALTER TABLE silver.signal_scores ADD COLUMN tier STRING;`
  (conviction tier sink write is best-effort until then).
- **Config bug (PC):** `hackernews.QUERIES` are still macro AI/semis terms —
  retune to P&C insurtech/cyber/cat. (macro's HN queries are correct.)
- **Dead branch (PC):** edgar `is_fund → 'fed_markets'` (macro residue; never fires).
- **macro obsidian.py** not yet on core's render/paths/archive primitives — next
  mechanical lift before the daily-note-hooks seam.
- **Pre-existing lint:** ~23 ruff nits in PC viz/collision/health/serff and ~8 in
  macro (unused vars, f-strings) — not bugs/security; sweep opportunistically.
- **Branches:** PC `digest-core-macro-port`, macro `digest-core-port` — review +
  merge to each `master`.
