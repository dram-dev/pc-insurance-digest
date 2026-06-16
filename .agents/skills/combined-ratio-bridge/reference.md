# Combined-ratio bridge reference — bases, AY-vs-CY, and the change decomposition

Deeper backing for [SKILL.md](SKILL.md). Read this for the precise definitions, the
two premium bases, the accident-year vs calendar-year mechanics, and the worked
period-over-period bridge.

## 1. Definitions (and what divides by what)

All ratios are a percentage of **net earned premium (NEP)** *except* the statutory
expense ratio, which uses **net written premium (NWP)**:

| Ratio | Numerator | Denominator |
|---|---|---|
| Loss ratio | incurred losses (calendar year) | NEP |
| LAE ratio | loss adjustment expense | NEP |
| Loss & LAE ratio | losses + LAE | NEP |
| Expense ratio (**GAAP**) | underwriting expenses | **NEP** |
| Expense ratio (**statutory / trade**) | underwriting expenses | **NWP** |
| Combined ratio | — | loss&LAE ratio + expense ratio |

Combined < 100 = underwriting profit; > 100 = underwriting loss. Underwriting result
in $ = `NEP × (1 − combined)`.

## 2. GAAP vs statutory/trade — why the same book shows two combined ratios

The only difference is the expense-ratio denominator:
- **GAAP** matches expenses to **earned** premium (accrual; what public carriers
  headline in 10-Ks).
- **Statutory / trade** divides expenses by **written** premium (cash-basis flavor;
  what NAIC annual statements and rating agencies use).

When written = earned (flat book) the two coincide. On a **growing** book written >
earned, so the statutory expense ratio is **lower** → statutory combined < GAAP
combined. On a shrinking book it's the reverse. Demo check: $280 expense on $1,000
earned = 28.0% (GAAP); on $1,040 written = 26.9% (statutory) → trade combined 91.9 vs
GAAP 93.0. **Never compare a GAAP combined to a statutory combined point-for-point**,
and when comparing two carriers confirm they're on the same basis.

## 3. Accident year vs calendar year — the development split

**Calendar-year (CY)** incurred losses = losses paid + change in reserves *during the
period*, regardless of which accident year they belong to. So CY includes **prior-year
development**: if reserves for old accident years are strengthened (adverse) or
released (favorable) this period, it lands in CY losses.

**Accident-year (AY)** losses = losses *belonging to* the current period's accidents,
at current estimate — it excludes development on prior years.

```
CY losses = AY (current) losses + prior-year development
⇒ AY combined = CY (reported) combined − pyd_ratio
```

Sign convention: PYD `+` adverse (raises CY), `−` favorable (lowers CY).
- **Favorable** development (`−`) makes the *reported* (CY) combined look better than
  the current accident year really is → AY combined > reported. This is the
  "flattered by releases" pattern.
- **Adverse** development (`+`) makes reported worse than the current AY → AY combined
  < reported.

## 4. The underlying combined — the margin signal

Strip **both** cats and development from the reported combined to get the current
accident-year, ex-cat margin:

```
underlying = reported − cat_ratio − pyd_ratio
           = (loss&LAE − cat − dev) + expense
underlying_loss&LAE = loss&LAE ratio − cat_ratio − pyd_ratio
```

This is the cleanest read of whether *current* pricing covers *current, normal* loss
cost. Carriers call it "underlying combined," "ex-cat ex-PYD combined," or "accident-
year ex-cat combined." It's what you trend to judge rate adequacy, because it removes
the two things management doesn't control quarter to quarter (cat incidence) or that
reflect the past (reserve adequacy).

## 5. Worked example (the script's `--demo`)

Inputs ($): NEP 1,000 · incurred loss 600 · LAE 50 · UW expense 280 · cat 80 · PYD
+20 (adverse) · NWP 1,040.

| Component | Value |
|---|---|
| loss ratio | 60.0% |
| LAE ratio | 5.0% |
| loss & LAE ratio | 65.0% |
| expense ratio (GAAP) | 28.0% |
| **combined ratio** | **93.0%** (UW profit 7.0 pts, $70) |
| cat load | 8.0% |
| prior-year development | +2.0% (adverse) |
| accident-year combined | 91.0% (= 93.0 − 2.0) |
| ex-cat combined | 85.0% (= 93.0 − 8.0) |
| **underlying combined** | **83.0%** (= 93.0 − 8.0 − 2.0) |

Bridge: underlying 83.0 + cat 8.0 + dev 2.0 = 93.0 reported. ✓

## 6. Worked period-over-period bridge

Decomposing the *change* in combined ratio is where this earns its keep — it answers
"the combined ratio moved N points; how much was real margin vs cat vs reserves?"

```
Δcombined = Δunderlying + Δcat + Δdevelopment
Δunderlying = Δ(underlying loss&LAE) + Δexpense
```

Demo bridge (prior → current, both GAAP): prior combined 93.0 → current 89.0, **−4.0
pts**. Current: NEP 1,100, loss 627, LAE 55, expense 297, cat 99, PYD −22 (favorable).

| Driver | Δ (pts) |
|---|---|
| Δ underlying | **−1.00** |
| &nbsp;&nbsp;of which Δ underlying loss & LAE | +0.00 |
| &nbsp;&nbsp;of which Δ expense | −1.00 |
| Δ cat load | +1.00 |
| Δ prior-year development | −4.00 |
| **Δ combined (check)** | **−4.00** |
| *memo:* calendar-year Δ loss & LAE | −3.00 |

Story: the 4-point improvement is **almost entirely a reserve-development swing**
(−4 pts: from +20 adverse to −22 favorable), partly offset by a heavier cat load
(+1). The *underlying* book barely moved (−1, all expense). A reader who looked only at
the calendar-year loss&LAE (−3) would wrongly credit improving loss experience —
the bridge shows it's reserves and cat luck.

## 7. Pitfalls checklist

- **Cat is a subset of incurred losses**, not additive — the script warns if cat >
  loss&LAE.
- **Don't pass both** `loss_lae` and (`incurred_loss` + `lae`) — pick the disclosure
  the carrier actually gives.
- **Reinsurance basis** — make sure all figures are net (or all gross); a net loss
  ratio with a gross expense base is meaningless.
- **PFAS/asbestos and large losses** sit inside development or AY losses depending on
  recognition; if a carrier breaks them out separately, decide whether to treat them
  like cats (one-off) or leave them in underlying, and say which.
- **Cat definition varies** (PCS threshold vs carrier-internal) — when comparing
  carriers, the cat-load lines may not be on the same definition.
- **The bridge assumes cat and PYD are disjoint** — if a carrier reports
  *cat development* (development on prior cats), it's in both the cat and PYD figures;
  net it out of one before feeding the bridge.

## 8. How this maps to the warehouse

- There is **no combined-ratio table**; inputs come from the carrier's EDGAR content
  (the pipeline's `_financial_excerpt` pulls combined ratio / premiums / net income,
  `_reserve_excerpt` pulls the reserve-development note) and investor supplements.
- The **development** line connects to [[reserving-chain-ladder]] /
  [[bornhuetter-ferguson]]: prior-year development in the combined ratio is the
  income-statement face of the same reserve move those skills estimate from triangles,
  and to `reserving_signals` / `disclosure_sentiment` in the warehouse.
- The **underlying loss-cost trend** connects to [[severity-trend-decomposition]] and
  the FRED cost-driver feed (`supply_chain`): rising underlying loss&LAE without rate
  is the early-warning the leaderboard's inflation-keyword boost is trying to surface.
- Feeds the `underwriting_results` topic and the LLM materiality judgment that drives
  the leaderboard's `llm_judgment` factor — a clean underlying-combined read is exactly
  the kind of systemic signal the summarizer prompt is told to err high on.
