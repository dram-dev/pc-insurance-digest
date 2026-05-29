# Mac-mini one-pager — your tasks (independent of code work)

Everything below runs on the Mac mini (live MLX/Ollama/Obsidian/Databricks).
The code is on `master` in both repos. Work top-to-bottom; each block is
independent enough to do in any sitting. ☐ = do it, then verify with the command.

---

## 1. Sync + env (5 min, once)
```bash
cd ~/Projects/pc-insurance-digest && git pull && uv sync
cd ~/Projects/macro-ai-digest    && git pull && uv sync
```
- ☐ **Ollama embedding model** (needed for `digest related/ask` + the follow-on outcome signal):
  `ollama pull nomic-embed-text`
- ☐ **`.env` (both repos)** — set the Databricks block (see `.env.example`):
  `DATABRICKS_ENABLED=true`, `DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`,
  `DATABRICKS_TOKEN`, `DATABRICKS_CATALOG=digest`.
  Schema prefix is preset: PC=`pc_`, macro=`macro_` (leave as-is).
- ☐ **(PC) `COURTLISTENER_TOKEN`** if you want the docket tracker live.
- Verify: `uv run digest sources` (PC) and `uv run digest sources` (macro) — both render the catalog.

## 2. Databricks DDL — apply in order (Free Edition SQL editor)
Shared catalog `digest`, domain-prefixed schemas. **First run `CREATE CATALOG IF NOT EXISTS digest;` then `USE CATALOG digest;`**
- ☐ PC: run `packages/digest-core/sql/databricks/bronze.sql`, then `silver.sql`, then `gold.sql`.
- ☐ macro: run `macro-ai-digest/sql/databricks/{bronze,silver,gold}.sql`.
- ☐ Cross-domain (after both above): `packages/digest-core/sql/databricks/xdomain.sql`.
- ☐ **Migration note:** the old un-prefixed `bronze`/`silver`/`gold` schemas (pre-rename) are now orphaned — `DROP SCHEMA ... CASCADE` them, or `CREATE TABLE pc_bronze.x AS SELECT * FROM bronze.x` to carry any data over.
- Verify: a pipeline run with `DATABRICKS_ENABLED=true` lands rows — `SELECT COUNT(*) FROM digest.pc_bronze.ingested_items;`

## 3. Run the new jobs (as data matures)
Daily `am`/`pm`/`weekly` launchd jobs already run ingest→triage→summarize→publish.
Add these (manually first, then schedule if you like):
- ☐ `uv run digest embed` — after a few ingest cycles (builds item vectors). Then try `uv run digest ask "FL FAIR Plan exposure"` and `uv run digest related <id>`.
- ☐ `uv run digest brief` — daily glance: regime + top signals + alert watchlist (works offline).
- ☐ `uv run digest outcomes` — **weekly**, once items have aged past 7d/30d (backtests whether ranked items mattered).
- ☐ `uv run digest learn` — once `outcomes` has ≥12 labeled items; prints the heuristic-vs-learned A/B and writes `learned_score`.
- ☐ `uv run digest reserving` — once loss triangles exist (see §4).
- Verify: `uv run digest calibration` (rate a few items first with `digest rate <id> <1-5>`).

## 4. Validate the disabled ingestors (curl was blocked in the cloud)
Each needs its live page checked + CSS/selectors confirmed, then `enabled:true`:
- ☐ `config/state_doi_sources.yaml` — CA first, then FL/TX/NY/LA (→ `regulatory_rate`).
- ☐ `config/serff_states.yaml` — confirm POST/search params per portal; CA (CDI) first.
- ☐ `collision_data.py` — validate CCC/Mitchell selectors (→ `supply_chain`).
- ☐ `industry_research_sources.yaml` — LexisNexis + JD Power selectors.
- ☐ `naic_schedp` + `investor_supp` — **these feed `digest reserving`**; validate, enable, then `digest reserving` produces `gold.reserving_signals` and the `reserve_deterioration_boost` auto-activates.
- Verify per source: `uv run digest ingest <name>` then `uv run digest sources` (watch its row go from `never-run`/`0` to live).

## 5. Databricks BI (UI, optional — Free Edition has Genie + Alerts)
- ☐ **Genie space** over `pc_gold` + `xdomain` views — ask "keep-rate by source this month?", "burden-intensity trend?".
- ☐ **AI/BI dashboard** tiles: `pc_gold.daily_leaderboard`, `topic_trend`, `burden_trend`, `source_quality`, `pipeline_slos`, `outcome_hit_rate`.
- ☐ **Alerts** — create one per query in `packages/digest-core/sql/databricks/alerts.sql` (trigger = "returns rows"): regime flip, high burden, TPLF/nuclear verdict, source degradation, FRED anomaly.

## 6. Optional activation decisions (after data flows)
- ☐ **`reserve_deterioration_boost`** auto-activates once `reserving_signals` has adverse rows — nothing to flip; confirm via `signal_scores.reserve_boost > 1.0` on affected insurers.
- ☐ **`learned_score`** populates on `signal_scores` after `digest learn`; it's **advisory only** (ranking stays on the heuristic). If the A/B (`gold.outcome_hit_rate`, heuristic vs learned) shows a sustained lift, tell me and I'll wire it into ranking behind a flag.
- ☐ **Scoring weights** — tune any boost in `${OBSIDIAN_VAULT_PATH}/81 P&C Digest/_meta/Scoring Weights.md` (re-read each `digest signals` run).

## 7. Known loose ends (low priority)
- ☐ **macro `gmail`** ingestor's last run errored (surfaced by `digest sources` ✗) — likely OAuth token refresh; re-run `digest ingest gmail` and re-auth if needed.
- ☐ **(PC) `hackernews.QUERIES`** still carry macro AI/semis terms — retune to P&C insurtech/cyber/cat (ask me).
- ☐ **AM Best / dead trade-press feeds** — revisit with a real browser User-Agent.

---
*Generated alongside the digest-core + Databricks-forward work. The roadmap +
status live in `packages/digest-core/SEAMS_PLAN.md` and `~/.claude/plans/`.*
