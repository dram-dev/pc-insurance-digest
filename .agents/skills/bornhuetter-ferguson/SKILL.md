---
name: bornhuetter-ferguson
description: >-
  Bornhuetter-Ferguson and Cape Cod loss reserving over the PC Digest warehouse.
  Use when chain-ladder is unstable — green/immature accident years, a thin latest
  diagonal multiplied by a large CDF, low-credibility development factors — or when
  you want to blend an a-priori expected loss (premium × ELR) with observed
  development. Produces BF ultimates and IBNR per accident year, a Cape Cod
  (Stanard-Bühlmann) data-derived ELR, and a side-by-side vs pure chain-ladder so
  you can see where and how much BF pulls a year back toward plan.
---

# Bornhuetter-Ferguson reserving

Chain-ladder develops a year by multiplying its **latest actual** by a **CDF**. For a
green accident year that CDF is large and the latest diagonal is thin, so one noisy
cell swings the ultimate wildly. **Bornhuetter-Ferguson** fixes this: trust the
actuals for the *developed* part, and an **a-priori expected loss** for the
*undeveloped* part.

```
IBNR_BF     = Apriori_ultimate × (1 − 1/CDF)     # % unreported × expected loss
Ultimate_BF = Latest_actual + IBNR_BF
Apriori     = premium × ELR          (ELR supplied, or derived via Cape Cod)
```

This is the companion to [[reserving-chain-ladder]] — same triangles, same
volume-weighted development math (so the CDFs reconcile), different IBNR step. Use
chain-ladder for mature years; reach for BF on the youngest 1–2 AYs and any
low-credibility factor. For the full theory, the worked example, Cape Cod
derivation, and when each method wins, read [reference.md](reference.md).

## Where the data lives

- **`loss_triangles`** — `(insurer, lob, metric, accident_year, dev_period,
  cumulative_value, as_of, canonical_lob)`; `metric` is `'paid'` or `'incurred'`.
  Same table the chain-ladder skill reads; now spans the top-10 SEC insurers and
  carries `canonical_lob` for cross-insurer comparison. The triangle gives
  development; for the **premium a-priori**, `insurer_xbrl_facts` dataset=
  `premiums` (field `premiums_earned_net`, by segment × fiscal year — calendar-year
  earned ≈ accident-year earned for a steady book; see
  `digest.fundamentals.earned_premium_by_segment`) or `statutory_facts` DPW for
  the mutuals. Segment ≠ a single triangle LOB, so map with judgment, or supply
  premium directly with `--premiums` / Cape Cod (`--cape-cod`).
- **`reserving_signals`** — the pipeline's stored chain-ladder ultimate/IBNR per key.
  BF should bracket this: BF and CL agree on mature AYs and differ on green ones.
- **`disclosure_sentiment`** — reserve tone (strengthening/releasing); cross-read the
  BF number against the narrative.

## Procedure

1. **Get the a-priori.** BF needs an expected loss for the undeveloped part. Two ways:
   - **Supply the ELR** (`--elr 0.72`): a plan/budget/prior-selected loss ratio, or
     an industry benchmark. Needs premium by AY too (`--premiums`).
   - **Derive it (Cape Cod)** (`--cape-cod`): `ELR = Σ actual / Σ (premium ×
     %developed)` — lets the triangle pick the ELR, removing the "where did 0.72 come
     from" objection. Still needs premium by AY.
   Pull premiums from the carrier's filing / investor supplement (net earned premium
   by accident/policy year for the line).

2. **Run it.** DB mode reads the latest snapshot read-only:
   ```bash
   python3 .Codex/skills/bornhuetter-ferguson/scripts/bornhuetter_ferguson.py \
       --db data/state.db --insurer PGR --lob personal_auto --metric incurred \
       --premiums "2021:30000,2022:33000,2023:35000,2024:37000" --elr 0.72 --cape-cod
   ```
   `--cape-cod` makes the derived ELR primary (a supplied `--elr` is still shown for
   comparison via the table when given alone). `--tail 1.05` for a long-tail line,
   `--format json` for structure, `--demo` runs the verified worked example, and
   `--stdin` takes an ad-hoc triangle: `echo '{"cells":[...],
   "premiums":{"2022":33000}, "elr":0.72}' | …`.

3. **Read the output, in order:**
   - **Factors + CDF** — identical to chain-ladder; the low-credibility flags tell you
     which AYs *should* lean on BF.
   - **ELR line** — the a-priori you used and its source; plus the Cape Cod ELR with
     its `Σactual / Σused-up-premium` math so the derivation is auditable.
   - **Per-AY table** — `%unrep` (= 1 − 1/CDF, the share BF takes from the a-priori),
     `apriori`, **CL ult**, **BF ult**, **BF IBNR**. The gap between CL and BF *is* the
     BF correction, concentrated in high-`%unrep` years.
   - **Totals** — CL vs BF ultimate and IBNR, and `BF − CL IBNR` in $ and %.

4. **Cross-check vs the pipeline.** Compare BF and CL totals to `reserving_signals`.
   The stored signal is pure CL; if BF is materially lower IBNR, the stored adverse/
   favorable read on the green years may be CL over-reaction, not a real reserve move.

## Interpreting the result (judgment, not arithmetic)

- **BF ≈ CL on mature AYs, diverges on green ones.** At `%unrep ≈ 0` the two are
  identical (nothing undeveloped to disagree about). The whole value of BF is in the
  youngest years where CL is least trustworthy — so always read the table top
  (mature, agree) to bottom (green, BF corrects).
- **BF inherits the a-priori's bias.** A too-low ELR makes BF under-reserve green
  years and *manufactures* favorable development next snapshot; too-high does the
  reverse. State the ELR and where it came from. Cape Cod removes the arbitrary pick
  but assumes the triangle's own historical loss ratio is the right expectation —
  bad if rate adequacy or mix shifted materially.
- **Cape Cod is the honest default** when you have no credible external plan ELR: it
  weights each AY's contribution by how developed (credible) it is. It collapses to a
  premium-weighted average loss ratio and sits between CL and a flat-ELR BF.
- **Same paid-vs-incurred discipline as chain-ladder.** BF IBNR on an **incurred**
  triangle is pure IBNR; on a **paid** triangle it is total unpaid (case + IBNR).
  Don't compare across metrics, and make sure the ELR basis matches the metric
  (an incurred ELR for an incurred triangle).
- **Tail still matters.** `%unrep` only counts development inside the triangle; a
  long-tail line past the triangle width needs a `--tail` > 1 or BF under-reserves
  the oldest years too.

## Output discipline

Lead with the BF number, the a-priori it rests on, and the contrast with CL on the
years that differ. E.g. *"PGR personal-auto incurred BF IBNR $1.20B vs chain-ladder
$1.43B (−16%); the gap is entirely AY2024 (44% unreported), where CL multiplies a thin
diagonal by a 1.8 CDF — BF anchors it to a Cape Cod ELR of 0.83, so the stored adverse
signal on the youngest year is likely CL over-reaction, not a real strengthening."*
