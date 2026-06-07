# Severity / loss-cost trend reference

Backing for [SKILL.md](SKILL.md).

## 1. Why log-linear (exponential) trend

Loss costs compound — a 6%/yr severity trend multiplies, it doesn't add. Fitting
`ln(value) = a + b·t` by OLS estimates a constant *proportional* growth rate;
`exp(b) − 1` is the annualized trend. Advantages over a linear fit: it can't
project negative values, the slope is scale-free (a percentage), and most loss-cost
series (CPI/PPI components, severity tapes) are closer to exponential than linear.
R² is reported on the log scale, so it measures how well a *constant percentage*
trend fits — a low R² is the signal to distrust a single trend number.

## 2. The trend period (for ratemaking hand-off)

Two trend legs matter when projecting losses for a rate indication:
- **Past trend** — from the average accident date of the experience period to
  "now" (or to the latest data point).
- **Future / projected trend** — from "now" to the average accident date of the
  *future* policy period the rates will cover.

The total trend factor is `(1 + annual_trend)^(trend_years)` over the full leg;
`trend_years` for an annual policy is often ~1.5–2.5. Feed the decomposed
loss-cost trend and the period length into `ratemaking-indication`'s
`--trend-annual` / `--trend-years`. Frequency and severity can trend at different
rates and over different periods — decompose, trend each, recombine.

## 3. Frequency × severity decomposition

```
  pure premium = frequency × severity            (loss cost per exposure)
  ⇒ 1 + PP_trend = (1 + freq_trend)(1 + sev_trend)
```

Interpretation of the four quadrants:
- **freq↑ sev↑** — broad deterioration (e.g. inflation + more accidents); urgent.
- **freq↓ sev↑** — the modern auto/liability pattern: fewer but costlier claims
  (safety tech cuts frequency, repair/medical/verdict inflation lifts severity).
  The **social-inflation fingerprint** on long-tail lines.
- **freq↑ sev↓** — often a mix shift or small-claim surge; check definitions.
- **freq↓ sev↓** — improving loss costs; rare in inflationary regimes.

## 4. Reading severity by component

Trend each `severity_index` category separately:
- **used_vehicle / parts / labor** → physical-damage (auto) severity; track
  Manheim/CPI-parts/repair-labor. Spikes feed collision/comprehensive.
- **medical** → bodily-injury and workers-comp severity.
- **blended** → the headline loss-cost tape; decompose into the above to explain
  *what* moved it, rather than reporting the blend alone.

## 5. Robustness & pitfalls

- **Outliers / cat distortion** — a cat-affected period inflates a severity fit;
  cap or exclude cats and trend the attritional series separately.
- **Mix shift** — if the book's composition changed, the index trends a moving
  target; segment if possible.
- **Series breaks / re-basing** — a methodology change in the underlying index
  shows up as a level jump, not a trend; fit within a consistent regime.
- **Leverage of recent points** — the latest, often-immature point can swing a
  short fit; report `last_vs_trend_pct` so the reader sees how much the tail
  pulls, and prefer enough points (≥ ~8–12) for a stable slope.
- **Seasonality** — use seasonally-adjusted series or full-year points; a trend
  fit on raw monthly data confounds season with trend.
- **Confidence** — a trend is an estimate; a wide scatter (low R²) or a short
  window means a wide interval. Pair a hot tape read with `disclosure_sentiment`
  and `litigation_pressure` rather than asserting causation from the index alone.
