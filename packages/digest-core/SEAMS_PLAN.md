# digest-core — extraction status & the (now-optional) design seams

## ✅ Foundation extraction COMPLETE (2026-05-29)

The shared core is extracted and **de-duplicated across both domains** —
pc-insurance-digest and macro-ai-digest both run on `digest_core`, and nothing
in the foundation layer is duplicated between the two repos anymore. Both
branches are merged to `master` (PC 136 tests, macro 22, all green).

| Module (in `digest_core`) | Status | How each domain consumes it |
|---|---|---|
| `types.IngestedItem` | ✅ | re-export |
| `db.{schema,helpers}` | ✅ | thin wrappers (default db_path + sink fan-out) + domain MIGRATIONS |
| `ingest.base` (IngestorBase + ItemStore) | ✅ | `class IngestorBase(CoreBase): store = db` |
| `ingest.registry` (auto-register via `__init_subclass__` + `discover`) | ✅ | `discover_ingestors("digest.ingest")` — drop a file, it's a source |
| `ingest.{rss,hackernews,reddit,edgar}` fetch | ✅ | thin shells hold config, delegate fetch() |
| `summarize.{backends,runner}` (+ `register_backend`) | ✅ | `_backend_config()`; shared `extract_json` |
| `obsidian.{paths,render,archive}` | ✅ | `Paths` subclass; topic maps + note layout stay domain |
| `cli.base` (load_ingestor, run_ingest, discover_ingestors) | ✅ | both CLIs route through it |
| `catalog` (`digest sources`) | ✅ | live source catalog in both domains |
| `sinks.databricks` (+ `schema_prefix`) | ✅ | singleton per domain; shared `digest` catalog, `pc_*`/`macro_*` schemas |

Tests: PC `tests/` (136), macro `tests/` (22) — both hermetic.

**The original goal is met.** "Both digests become thin plug-ins" is true at the
layer where it should be: the *framework* is shared; the *domain* logic
(signals factors, triage prompts/taxonomy, note layout, domain ingestors, and
macro's essays/debate/velocity/etc.) correctly stays in each repo.

## The remaining "seams" are OPTIONAL — do them reactively, not on a schedule

These are **design abstractions, not extractions**. They would trade a little
parallel-but-similar code for a more coupled, more abstract core. The project
rule *"don't pre-design the framework"* now points the other way: **don't lift a
seam until it actually bites** (e.g., you go to add a scoring factor and resent
editing both repos). Ranked by value-if-you-do-it:

| Seam | Verdict | Why |
|---|---|---|
| **signals factor registry** | highest value *if* it bites | PC (12 boosts) and macro (z-score/cluster) have genuinely different factor sets — a `ScoreFactor` registry shares the *mechanism*, not the factors. Lift it the next time adding a factor means editing both repos. |
| **triage engine** | low–med | Flow is shared, `extract_json` already is; only the ~15-line run loop remains. Prompts/topics/auto-keep are correctly domain. |
| **obsidian `DailyNoteBuilder`** | low–med | Render primitives/Paths/archive already shared; only the note *layout* differs (and it should). |
| **regime N-axis** | **won't do** | Confirmed not peers — PC 2-axis (1 LLM-judged) timestamp-keyed w/ hysteresis/override/staleness vs macro 1-axis mechanical ISO-week. Regime stays domain-specific in both. |
| **topic-cap / materiality** | ~done | Mechanics (`enforce_topic_caps`) already in core; only the anchor text is domain. |
| **CLI group-factory** | low | Command sets have diverged (PC: reserving/learn/outcomes; macro: debate/essay/dashboard/velocity) — a shared factory would fight that. |

If you do pick one up, the design notes that were here are preserved in git
history (pre-2026-05-29 revisions of this file).

## Adding a new source (the organic-growth workflow — already live)

In either repo: add `src/digest/ingest/<source>.py`, subclass `IngestorBase`,
give it a `name`. It self-registers → appears in `digest sources`, is runnable
via `digest ingest <source>`, joins the pipeline. No central dict to edit. A new
LLM backend: `register_backend("name", fn)`. Missing-optional-dep modules are
reported by `digest sources`, not silently dropped.

## Beyond the foundation (PC domain features shipped on top, 2026-05-29)

The Databricks-forward roadmap (`~/.claude/plans/`) shipped end-to-end on PC:
Enabler 0 (cross-domain sink) + Options 1a (calibration), 1b (outcome backtest),
2 (`digest brief` + gold views + alerts), 3 (embeddings/`ask`), 4 (learned
scorer), 5 (chainladder reserving), and the reserve/learned signals wired into
scoring (neutral until their data flows). These are PC *domain* features, not
core seams — listed here only so the era's scope is on record. Operator steps
live in the repo-root `MAC_MINI_TASKS.md`.

## Loose ends carried (not blockers)
- **macro `gmail`** last run errored (surfaced by `digest sources` ✗) — likely OAuth refresh.
- **(PC) `hackernews.QUERIES`** still macro AI/semis terms — retune to P&C.
- **Dead branch (PC):** edgar `is_fund → 'fed_markets'` (macro residue; never fires).
- **Pre-existing lint:** ~23 ruff nits in PC viz/collision/health/serff, ~8 in macro — not bugs; sweep opportunistically.
- **Pre-existing deprecation:** `datetime.utcnow()` in the core sink (4 call sites) — harmless; swap to `datetime.now(UTC)` when convenient.
