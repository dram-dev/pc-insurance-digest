---
name: combined-ratio-bridge
description: >-
  Decompose a P&C combined ratio into loss / LAE / expense, strip catastrophes and
  prior-year reserve development, and expose the underlying (current accident-year,
  ex-cat, ex-development) combined ratio. Use for any underwriting-results question —
  reading a carrier's 10-K/10-Q/8-K combined ratio, comparing two periods (the
  quarter-over-quarter "why did the combined ratio move" question), separating real
  margin change from cat luck and reserve releases, or converting between GAAP
  (earned-premium) and statutory/trade (written-premium) expense bases.
---

# Combined-ratio bridge

A headline combined ratio hides three very different things: the **underlying**
underwriting margin (current accident-year, ex-cat), the **catastrophe** load (luck),
and **prior-year reserve development** (borrowing from or repaying the past). This
skill splits them, so you can answer "did underwriting actually get better, or did the
quarter just have light cats and a reserve release?" — the question behind the
`underwriting_results` topic. The helper does the arithmetic and enforces the
identities; you supply judgment.

```
reported combined = underlying + cat load + prior-year-development
accident-year combined = reported − prior-year-development      (ex-development)
underlying = reported − cat load − prior-year-development       (current-AY ex-cat)
```

The inputs are dollar figures — earned premium and incurred losses & LAE. The
warehouse now carries them as structured XBRL facts: `insurer_xbrl_facts`
dataset=`premiums` (field `premiums_earned_net`) and dataset=`combined_ratio`
(field `losses_and_lae_incurred`), by segment × period, for the top-10 SEC
insurers (`fundamentals(ticker)` or `digest.fundamentals.combined_ratio_components`).
Cats and prior-year development still come from the EDGAR text the pipeline
extracts (`_financial_excerpt` / `_reserve_excerpt`); pair with
[[bornhuetter-ferguson]] when the development line needs its own reserve view.
For the bases, the AY-vs-CY mechanics, and the YoY/QoQ bridge, see
[reference.md](reference.md).

## What you need (from the filing)

Per period, in dollars:
- **earned premium** (NEP) — denominator for loss and GAAP expense ratios. *Required.*
- **incurred loss** (calendar-year, includes cat + development) — or pass `loss_lae`
  if the carrier only gives a combined loss&LAE figure.
- **LAE** (loss adjustment expense), if reported separately.
- **underwriting expense** (acquisition + general expenses).
- **written premium** (NWP) — only needed for a **statutory/trade** expense ratio.
- **prior-year development** ($, **signed**: `+` adverse / strengthening, `−`
  favorable / release).
- **catastrophe losses** ($, the cat portion of incurred losses).

Carriers disclose these in different combinations; take what's given and note any
component you had to assume (e.g. cat = 0 if not disclosed).

## Procedure

1. **Pin the basis.** GAAP combined (expense on **earned**) is what most carriers
   headline; statutory/"trade" combined (expense on **written**) is what state filings
   and rating agencies use. They differ whenever written ≠ earned (a growing book has
   written > earned → lower statutory expense ratio). Use `--basis gaap` (default) or
   `--basis statutory`.

2. **Single period:**
   ```bash
   python3 .claude/skills/combined-ratio-bridge/scripts/combined_ratio_bridge.py \
       --earned-premium 21500 --incurred-loss 14600 --lae 1900 \
       --underwriting-expense 4400 --prior-year-development -445 \
       --cat-losses 1750 --basis gaap
   ```
   `--demo` runs a verified worked example; `--format json` for structure; `--stdin`
   takes a JSON period.

3. **Period-over-period bridge** (the QoQ / YoY "what moved the combined ratio"
   decomposition) — pass `current` and `prior` on stdin:
   ```bash
   echo '{"basis":"gaap",
     "current":{"earned_premium":...,"incurred_loss":...,"cat_losses":...,"prior_year_development":...},
     "prior":  {"earned_premium":...,"incurred_loss":...,"cat_losses":...,"prior_year_development":...}}' \
     | python3 .claude/skills/combined-ratio-bridge/scripts/combined_ratio_bridge.py --stdin
   ```
   Output attributes the change in combined ratio to **Δunderlying** (split into
   Δunderlying-loss&LAE and Δexpense), **Δcat**, and **Δdevelopment** — which sum to
   Δcombined exactly.

4. **Read it top-down:** combined → its quality (cat load, development direction) →
   the bridge (underlying + cat + dev) → AY-vs-CY. The number to *lead with* is the
   **underlying combined**, not the headline.

## Interpreting the result (judgment, not arithmetic)

- **Underlying is the signal; headline is noisy.** A 95 combined that is 88 underlying
  + 9 cat − 2 favorable dev is a *strong* book having a heavy-cat quarter. A 95 that is
  99 underlying + 1 cat − 5 favorable dev is a *deteriorating* book flattered by
  reserve releases — same headline, opposite story. The pipeline's
  `reserve_deterioration_boost` and the `reserving` topic care about exactly that
  development line.
- **Favorable development is borrowing from the past.** Negative PYD lowers the
  reported combined but means prior reserves were redundant; it can't repeat forever.
  When AY (ex-development) combined is *worse* than reported, the current accident year
  is deteriorating under the cover of releases — the watch-item pattern.
- **Mind the basis when comparing carriers.** A GAAP combined and a statutory combined
  are not comparable point-for-point; growth widens the gap. State which you used.
- **Cat is a subset of incurred, not additive.** `cat_losses` should already be inside
  `incurred_loss`; the script warns if cat > total loss&LAE (a sign you double-counted).
- **Loss&LAE vs loss-only.** If the carrier reports a combined loss&LAE ratio, pass
  `loss_lae`; if it splits them, pass `incurred_loss` + `lae`. Don't pass both.
- **The bridge's calendar-year Δloss&LAE is a trap.** It moves with development and
  cats, not just pricing; the script reports it only as a memo. Trend the
  *underlying* loss&LAE.

## Output discipline

Lead with underlying vs headline and the reason for the gap, and keep the identity
honest (underlying = reported − cat − development). E.g. *"PGR Q1 combined 86.2 — but
underlying (current-AY ex-cat) was ~87.6: the quarter booked ~$445M (≈2 pts) of
favorable personal-auto development against a light ~0.6-pt cat load, so
underlying = 86.2 − 0.6 + 2.0 ≈ 87.6. The ~1.4-pt headline flattery is the reserve
release, not a step-change in current pricing margin — watch underlying, which held
roughly flat QoQ."*
