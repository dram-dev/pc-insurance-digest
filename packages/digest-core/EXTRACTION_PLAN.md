# `digest-core` Extraction Plan

A concrete what-moves-where map for cutting pc-insurance-digest +
macro-ai-digest apart into `digest-core` (framework) and two domain
plug-ins. Authored after Wave 2 of pc-insurance-digest shipped, while
the divergence between the two domains is freshly visible.

> **Status:** plan only. Do not extract until the ≤ 1-week PC Digest
> dogfooding window closes. The Wave 2 features (regime, signals,
> sonar) are still settling — running them in production may reveal
> additional seams or merge seams I've marked as separate.

## 1. Definitive: moves to `digest-core`

These have no domain coupling. Direct lift-and-drop.

| Source (pc-insurance-digest) | Target (digest-core) | Notes |
|---|---|---|
| `src/digest/ingest/base.py` (`IngestorBase`, `IngestedItem`) | `digest_core/ingest/base.py` | Verbatim. Already domain-agnostic. |
| `src/digest/ingest/rss.py` (`RSSIngestor`) | `digest_core/ingest/rss.py` | Config file (`rss_feeds.yaml`) stays in domain. |
| `src/digest/ingest/reddit.py` | `digest_core/ingest/reddit.py` | Subreddit list stays in domain. |
| `src/digest/ingest/substack.py` | `digest_core/ingest/substack.py` | Author list stays in domain. |
| `src/digest/ingest/hackernews.py` | `digest_core/ingest/hackernews.py` | Keyword list stays in domain. |
| `src/digest/ingest/edgar.py` | `digest_core/ingest/edgar.py` | Ticker universe stays in domain. |
| `src/digest/db.py` — base schema (items, run_log, summarizer_log) | `digest_core/db/schema.py` | Two-table base + summarizer log. |
| `src/digest/db.py` — `get_conn`, `init_db`, `upsert_items`, `log_run`, `item_stats`, `recent_items`, `recent_kept_titles` | `digest_core/db/helpers.py` | Domain-agnostic CRUD. |
| `src/digest/db.py` — migration runner pattern | `digest_core/db/migrations.py` | Idempotent ALTER pattern. Domains add their own migrations on top. |
| `src/digest/summarize.py` — `_call_claude_cli`, `_call_haiku_api`, `_call_gemini_flash`, `_call_local_qwen`, `_call_mlx_local`, `BACKENDS` registry, `BackendError` | `digest_core/summarize/backends.py` | Backend functions are pure transport — no domain content. |
| `src/digest/summarize.py` — `_extract_json`, `_enforce_topic_caps` | `digest_core/summarize/runner.py` | Generic JSON repair + share-cap mechanics. |
| `src/digest/summarize.py` — `SummaryOutput` dataclass shape | `digest_core/summarize/output.py` | Topic field is plain string — domains validate against their own taxonomy. |
| `src/digest/health.py` | `digest_core/health.py` | Pattern is generic. PC-specific launchd job names stay in domain wrapper. |
| `src/digest/security.py` | `digest_core/security.py` | Verbatim. |
| `src/digest/obsidian.py` — `Paths`, `_safe`, `_wikilink`, `_chat_link`, `_parse_see_also`, `_confidence_badge`, `append_run_log`, item/topic archive markers + writer | `digest_core/obsidian/{paths,render,archive}.py` | The shell + idempotent topic archives are generic. |
| `src/digest/cli.py` — Click group, `_load`, `_setup_logging`, generic commands (`ingest`, `stats`, `recent`, `health`, `init-db`) | `digest_core/cli/base.py` | Domain plug-ins import the group factory and register their `INGESTORS` map. |

## 2. Definitive: stays in `pc-insurance-digest`

These are the PC-specific concerns that prove the framework worth extracting.

| File / area | Why domain |
|---|---|
| `src/digest/triage.py` — `TOPICS` (17 P&C buckets), `SUB_TAGS`, `SYSTEM_PROMPT`, `INSURER_TICKERS_WAVE1`, `MANDATORY_FORM_TYPES`, `CAT_AUTO_KEEP_SOURCES` | P&C taxonomy + Wave 1/2 auto-keep rules. Macro digest's prompt + topics will look completely different. |
| `src/digest/ingest/nhc.py`, `usgs.py`, `spc.py`, `nifc.py` | CAT-event ingest is PC-only. Macro digest has no use for hurricane advisories or earthquake feeds. |
| `src/digest/db.py` — `auto_keep_insurer_filings`, `auto_keep_cat_events`, `auto_keep_usgs_major` | PC-specific auto-keep semantics (EDGAR insurer 8-K, NHC advisories, M ≥ 6 quakes). |
| `src/digest/db.py` — `cat_load_counts`, `items_for_market_cycle` | Reads the PC regime detector's inputs. |
| `src/digest/summarize.py` — `SYSTEM_PROMPT` (P&C reader persona, 17 topics, materiality rubric weighted toward personal-lines auto + fire) | Domain-specific reader framing and materiality anchors. |
| `src/digest/summarize.py` — `TOPIC_CAP_PCT = {"ai_insurtech": 0.35}` | The mechanism is framework; this *value* is PC-specific. |
| `src/digest/regime.py` — `MARKET_CYCLE_MULT`, `CAT_LOAD_MULT`, `MARKET_CYCLE_SYSTEM_PROMPT`, `compute_cat_load` | Two-axis PC regime. Macro's regime axes are different (one axis: macro_regime table already in db.py reflects that). |
| `src/digest/signals.py` — `SOURCE_MULT` table, `TOPIC_PRIORITY_BOOST`, `BURDEN_INTENSITY_BOOST`, `_topic_relevance` | Per CLAUDE.md "Source multipliers" + "Topic priority emphasis": these are PC-specific value tables on top of a generic scoring formula. |
| `src/digest/obsidian.py` — `TOPIC_LABELS`, `TOPIC_CALLOUT`, `TOPIC_EMOJI`, `TOPIC_ORDER` | P&C display labels. Macro digest has its own. |
| `src/digest/obsidian.py` — `_render_regime_callout`, `_render_sonar_callout` | PC two-axis callout + Sonar burden one-liner. |
| `config/rss_feeds.yaml`, `config/edgar_tickers.yaml`, `config/regime_override.yaml` | Domain configuration. |

## 3. Tricky seams (design work needed at extraction)

These are the places where the line between core and domain isn't obvious
yet. Defer the abstraction decision until extraction — implementing them now
risks pre-designing the wrong shape.

### 3.1 Regime detector — N-axis abstraction

**Tension:** PC has two axes (`market_cycle × cat_load`), macro digest has one
(`macro_regime` table in current `db.py`). Both need: hysteresis, manual
override, staleness check, LLM judgment + mechanical inputs.

**Possible shape:**
```
class RegimeAxis(ABC):
    name: str
    states: dict[str, float]   # state name → multiplier
    def compute(self) -> str:   # returns chosen state
        ...

class RegimeDetectorBase:
    axes: list[RegimeAxis]
    def compute_regime(self) -> RegimeSignal: ...
    def is_stale(self, hours: int) -> bool: ...
```

PC domain registers `MarketCycleAxis` (LLM-judged) + `CatLoadAxis` (mechanical).
Macro domain registers one axis. The hysteresis + override mechanics + table
schema live in core.

**Defer:** confirm macro's regime axis really *is* a peer of PC's
`market_cycle` before locking the interface. There may be a simpler base
class than `RegimeAxis` if macro's regime is essentially one-state-per-week
(unlike PC's continuous classification).

### 3.2 Signal leaderboard — formula composition

**Tension:** PC's formula is
`source × regime × topic_relevance × recency × llm_judgment × topic_priority × burden_intensity`.
Macro's will share `source × recency × llm_judgment` but may want different
domain-specific factors (e.g. `signal_outcomes` z-score, `cluster_id` size).

**Possible shape:** a registry of `ScoreFactor(name, fn(item, regime) -> float)`
that the core multiplies together. Domains register their own factors;
core provides the common ones (recency, llm_judgment, source_mult lookup
against a domain-provided table).

**Defer:** wait to see what factors macro's port actually needs. If it's just
"same formula, different numbers," a config-driven approach works without a
plugin system.

### 3.3 Triage prompt + auto-keep

**Tension:** the triage *flow* (Python preprocessor → Ollama → JSON parse →
DB update) is generic. The prompt content + auto-keep rules are entirely
domain.

**Possible shape:** core provides `TriageEngine` with hooks for:
- `domain_system_prompt: str`
- `auto_keep_steps: list[Callable[[], int]]`  (run before LLM)
- `verdict_normalizer: Callable[[dict], dict]`  (validate domain fields)

PC's `burden_direction/burden_intensity` validation goes in the domain
normalizer; core only enforces `decision/score/topic` shape.

**Defer:** confirm macro's triage normalize step doesn't have surprising
domain-specific fields (sub_tags structure, etc.) before locking.

### 3.4 Obsidian topic archives + daily / weekly note

**Tension:** the *mechanics* (frontmatter, daily note shell, topic archive
with marker-block upsert, weekly synthesis sections) are generic. The
*content* (topic labels, callouts, emojis, ordering, regime callout shape,
sonar callout shape) is domain.

**Possible shape:** core provides `DailyNoteBuilder` with extension points
for "extra top-of-note callouts" (PC adds regime + sonar; macro might add
its own market snapshot). Core handles topic grouping; domain provides the
`topic_label / topic_callout / topic_emoji / topic_order` maps.

**Defer:** decide whether `top_signals` leaderboard section is core or
domain. It feels core but the per-source quality table assumes
domain-specific source weights.

### 3.5 Per-topic share caps + materiality anchoring

**Tension:** capping noisy topics is generic; *which* topic gets capped is
domain. Same for materiality anchors ("personal-lines auto + fire = high").

**Possible shape:** core takes `TOPIC_CAP_PCT` as a constructor arg.
Materiality prompt block becomes a `domain_materiality_anchor: str` that
the core summarize prompt template interpolates.

**Defer:** verify macro digest actually wants per-topic caps before
extracting the mechanism. If only PC needs them, leave the cap in domain.

## 4. Open questions to settle at extraction time

- **Monorepo vs. separate repo for `digest-core`?** Currently scaffolded
  inside `pc-insurance-digest/packages/`. Easy to relocate later. Vote
  before extraction: if both domain projects are owned by the same user,
  a uv workspace in a `digests/` super-repo may be cleaner than three
  separate repos.
- **Python version + dependency floor.** Both PC and macro target ≥ 3.11
  today. Pin core to the same.
- **DB schema ownership.** Core owns `items`, `run_log`, `summarizer_log`.
  Each domain owns its own additional tables (PC: `regime_signals`,
  `signal_scores`, `cat_event` auto-keep helpers; macro: `macro_regime`,
  `signal_outcomes`, etc.). Migrations need a clean "core ran its
  migrations → domain runs additional migrations" sequencing.
- **`config.py` settings.** Core has a `BaseSettings` with `db_path`,
  `obsidian_vault_path`, etc. Domain extends with its own settings
  (insurer tickers path, etc.). Need to confirm pydantic-settings
  supports clean subclassing across packages.
- **Testing scaffold.** Neither project has a test suite today.
  Extraction is a forcing function — at minimum, regression-test the
  pipeline end-to-end against a fixture DB.

## 5. Sequencing when extraction begins

1. **Cut over PC Digest only first.** macro-ai-digest stays untouched until
   PC is reliably running on `digest-core`. Avoids a two-project breakage
   window.
2. **Per-module order:** db.helpers → ingest.base → ingest.{rss,reddit,…} →
   summarize.backends → summarize.runner → obsidian.{paths,render} →
   cli.base. Each cut produces a working PC Digest before moving on.
3. **Last:** the design-needed seams (regime, signals, triage normalize,
   daily-note extension hooks). These are where we'll do the only real
   *new* design work — everything else is moving code.
4. **Then** port macro-ai-digest onto core. Any seam that breaks during
   macro port goes back through one more round of design.
