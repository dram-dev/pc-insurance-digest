---
name: ratemaking-indication
description: >-
  Indicated rate change for a P&C book — loss-ratio and pure-premium methods.
  Use when a question involves rate adequacy, rate indications, rate filings,
  indicated vs filed rate change, on-leveling, loss or premium trend, permissible
  loss ratio, expense provisions, or target underwriting profit. Computes the
  overall indicated rate level change and reconciles it against SERFF-filed
  changes in the warehouse.
---

# Ratemaking — the indicated rate change

The fundamental insurance equation says premium must cover losses + LAE +
expenses + a target profit. Ratemaking inverts it to ask: **given projected
losses and the expense/profit structure, how much must the rate level change?**
The answer is the *indicated* change; what a carrier actually files (the *selected*
change, e.g. a SERFF filing) is usually tempered by competition, regulation, and
rate-capping. The gap between indicated and selected is itself a signal.

The helper computes the indication; you supply judgment on the inputs and read
the result against the filed actions. Verified against the Werner & Modlin
worked values. Deeper theory, on-leveling mechanics, and the two methods'
equivalence proof are in [reference.md](reference.md).

## The two methods (they give the same answer)

**Loss-ratio method** — works in *ratios*, used when you have premium but not a
clean exposure count (most real books):

```
                         L&LAE ratio + fixed-expense ratio
  indicated factor  =  ───────────────────────────────────────
                       1 − variable-expense ratio − target profit
  indicated change %  =  factor − 1
```

- **L&LAE ratio** = projected ultimate losses+LAE ÷ premium *at current rate
  level*. Project by trending and developing reported losses and **on-leveling**
  the premium (restating historical premium as if today's rates had been in
  force — else you measure rate adequacy against stale prices).
- **Fixed-expense ratio** = fixed expenses (don't vary with premium: e.g. policy
  issuance) ÷ premium. **Variable-expense ratio** = commissions, premium tax,
  etc. (scale with premium). **Target profit** = the UW profit & contingencies
  provision (e.g. 5%). The denominator is the **permissible loss ratio** — the
  share of premium left for losses after variable costs and profit.

**Pure-premium method** — works in *dollars per exposure*, used when you have a
clean exposure base (preferred for class ratemaking):

```
                   projected pure premium + fixed expense per exposure
  indicated rate = ────────────────────────────────────────────────────
                       1 − variable-expense ratio − target profit
  indicated change % = indicated rate / current average rate − 1
```

where projected pure premium = trended, developed ultimate L&LAE ÷ exposures.

## Worked example (the helper's self-test)

L&LAE ratio 0.65, fixed-expense ratio 0.06, variable 0.25, target profit 0.05:

```
factor = (0.65 + 0.06) / (1 − 0.25 − 0.05) = 0.71 / 0.70 = 1.01429  →  +1.43%
```

Pure-premium check (same expense structure): PP 300, fixed/exposure 20, current
average rate 450 → indicated rate = (300+20)/0.70 = 457.14 → **+1.59%**.

## Run it

```bash
# headline ratios
python3 .claude/skills/ratemaking-indication/scripts/ratemaking_indication.py \
  --method loss_ratio --loss-lae-ratio 0.65 --fixed-expense-ratio 0.06 \
  --variable-expense-ratio 0.25 --target-profit 0.05

# from building blocks (auto-applies development × trend × on-level):
python3 .../ratemaking_indication.py --method loss_ratio \
  --losses 6500 --premium 10000 --development 1.05 --trend-annual 0.08 \
  --trend-years 1.5 --on-level 0.98 --fixed-expense-ratio 0.06 \
  --variable-expense-ratio 0.25 --target-profit 0.05

# pure-premium, or pipe a JSON param object with --stdin / --format json
```

## Sourcing inputs from the warehouse

There is no premium/exposure table, so assemble inputs from primary disclosures:
- **Trend** — pull the loss-cost trend from the `severity-trend-decomposition`
  skill (`severity_index` / FRED), or from filings. Frequency × severity →
  pure-premium trend feeds `--trend-annual`.
- **L&LAE ratio, expense ratio, combined ratio, target margin** — from EDGAR
  filing content (`items` where source='edgar', the carrier's ticker) and
  investor supplements. Combined ratio − 100% is roughly the rate inadequacy if
  expenses/trend are flat (a fast sanity check on the indication's sign).
- **Filed/selected change** — `serff` items carry `metadata.rate_change_pct` and
  `state`; the `regulatory_rate` topic captures DOI actions. Compare your
  *indicated* to the *filed*:
  ```sql
  SELECT state, json_extract(metadata_json,'$.rate_change_pct') filed_pct, title
  FROM items WHERE source='serff' AND title LIKE '%<carrier>%' ORDER BY ingested_at DESC;
  ```

## Interpreting the result

- **Indicated > filed** → the carrier is under-pricing relative to its own loss
  pick (regulatory drag, competitive hold, or rate-capping). Sustained, this
  erodes the combined ratio with a lag → watch reserving and underwriting_results.
- **Indicated < filed** → filing ahead of the indication (rebuilding margin,
  catching up after suppressed years).
- **Sign sanity:** a combined ratio meaningfully above 100% with flat expenses
  implies a positive indication; if your computed indication disagrees, re-check
  trend, development, and on-level before trusting it.
- **Garbage-in caveat:** the indication is only as good as the loss projection.
  State trend period, development basis, and on-level assumptions explicitly;
  a thin or green experience period needs Bornhuetter-Ferguson losses and a
  credibility weight (see the `credibility-weighting` skill) before it drives a
  rate.
