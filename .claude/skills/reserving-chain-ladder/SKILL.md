---
name: reserving-chain-ladder
description: >-
  Chain-ladder loss reserving over the PC Digest warehouse. Use when a question
  involves loss reserving, loss triangles, development factors (LDF/CDF), IBNR,
  ultimate losses, reserve adequacy, or adverse vs favorable development for an
  insurer/line of business. Turns a cumulative paid/incurred triangle
  (loss_triangles) into LDFs, CDFs, per-accident-year ultimates and IBNR with
  credibility caveats, and cross-checks the pipeline's stored reserving_signals.
---

# Chain-ladder loss reserving

Develop a cumulative loss triangle to **ultimate losses** and read **IBNR** and
**adverse vs favorable development** — the core actuarial signal behind the
`reserving` topic. This skill is the method; the helper script does the arithmetic
(verified against the production roll-up in `src/digest/reserving.py` and a
hand-worked triangle), and you supply the actuarial judgment.

For the theory, the fully worked numeric example, factor-selection choices, tail
fitting, Mack uncertainty, and Bornhuetter-Ferguson, read
[reference.md](reference.md). This file is the operating procedure.

## Where the data lives

- **`loss_triangles`** — `(insurer, lob, metric, accident_year, dev_period,
  cumulative_value, as_of)`. `metric` is `'paid'` or `'incurred'`. Cumulative,
  upper-left triangle observed. One `as_of` per snapshot (e.g. a 10-K filing).
- **`reserving_signals`** — the pipeline's *own* chain-ladder output per key:
  `ultimate, latest, ibnr, prior_ibnr, deterioration_pct, direction`. This is
  your ground-truth cross-check and the source of the prior-period IBNR.
- **`disclosure_sentiment`** — reserve *tone* read over EDGAR filings
  (`reserve_tone` strengthening/releasing, `adverse_language_score`). Triangulate
  the number against the narrative.

## Procedure

1. **Scope the key.** Find available triangles before developing one:
   ```sql
   SELECT insurer, lob, metric, as_of, COUNT(*) cells
   FROM loss_triangles GROUP BY insurer, lob, metric, as_of
   ORDER BY as_of DESC, cells DESC;
   ```
   (Run via the `agent-server` `run_sql` tool, or `sqlite3 -readonly data/state.db`.)

2. **Develop it.** Run the helper — DB mode reads the latest snapshot read-only:
   ```bash
   python3 .claude/skills/reserving-chain-ladder/scripts/chain_ladder.py \
       --db data/state.db --insurer PGR --lob commercial_lines_liability \
       --metric incurred
   ```
   Add `--as-of 2024-12-31` for a specific snapshot, `--tail 1.05` for a tail
   factor, `--format json` for structured output. For an ad-hoc triangle (not in
   the DB), pipe JSON: `echo '{"cells":[{"ay":2019,"dev":0,"value":1000}, …]}' |
   python3 …/chain_ladder.py --stdin`.

3. **Read the output, in order:**
   - **LDFs** (age-to-age factors): each `dev j→j+1`, volume-weighted, with the
     simple-average alternative, the **CV** (link-ratio volatility), and **n**
     (how many ratios it rests on). A stable factor has low CV and adequate n.
   - **CDFs**: cumulative factor from each dev to ultimate (latest column = the
     tail). The CDF at a green year's latest dev is what magnifies its IBNR.
   - **Per-AY projection**: latest diagonal × CDF = ultimate; ultimate − latest =
     IBNR. The youngest accident years carry the most IBNR and the most
     uncertainty (highest CDF, least data).
   - **Totals + warnings**: total ultimate/IBNR, and credibility/data flags.

4. **Cross-check against the pipeline.** Compare totals to the stored estimate
   and pull the prior to classify development:
   ```sql
   SELECT ultimate, latest, ibnr, prior_ibnr, deterioration_pct, direction, as_of
   FROM reserving_signals
   WHERE insurer=? AND lob=? AND metric=? ORDER BY as_of DESC LIMIT 2;
   ```
   They should match to rounding for a clean triangle. If they diverge, the
   triangle changed (re-stated, new snapshot) or you applied a tail the pipeline
   didn't — say which.

## Interpreting the result (judgment, not arithmetic)

- **IBNR means different things by metric.** On a **paid** triangle, ultimate −
  latest = *total unpaid* (case + pure IBNR). On an **incurred** triangle it is
  *pure IBNR* (case reserves are already inside the incurred figure). Never
  compare a paid-IBNR to an incurred-IBNR as if equal.
- **Adverse development** = current ultimate/IBNR estimate is *higher* than the
  prior estimate for the same accident years (`direction='adverse'`,
  `deterioration_pct > 0`). It is the warning sign — reserves were too low. It
  feeds `reserve_deterioration_boost` and the `reserving` topic priority.
- **Credibility first.** A factor on n=1 link ratio (always true for the oldest
  dev step) or a high-CV column is fragile — the youngest AYs lean hardest on
  exactly those weak factors. Flag low-credibility cells; don't present a green
  year's IBNR as precise. Prefer Bornhuetter-Ferguson for very green years
  (see reference.md) — pure chain-ladder over-reacts to a thin latest diagonal.
- **Tail factor matters for long-tail lines.** Liability/WC develop well past the
  triangle's width; a tail of 1.0 understates ultimate. Note when the line is
  long-tail and the triangle is too short to capture the tail.
- **Watch confounders:** a factor < 1.0 (downward development), changing
  case-reserving adequacy, large losses distorting one AY, calendar-year effects
  (inflation/law change hitting all AYs at one diagonal). The script flags the
  first; you reason about the rest.

## Output discipline

Lead with the number and its development direction, then the credibility caveat,
then the mechanism. E.g. *"PGR commercial liability incurred IBNR $125.7M, ~0.5%
of latest, **favorable** (prior $193.3M, −35%); but the 1→2 and 2→3 factors rest
on ≤2 ratios, so the youngest two AYs are low-credibility — treat the release as
tentative until the next snapshot."*
