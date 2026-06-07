---
name: severity-trend-decomposition
description: >-
  Loss-cost trend analysis — fit an annualized exponential trend to a severity
  index and decompose pure-premium trend into frequency × severity. Use when a
  question involves loss-cost inflation, severity trend, frequency trend, claims
  inflation, social inflation in severity, the loss-cost tape, or projecting
  trend for a rate indication. Reads the warehouse severity_index / FRED series.
---

# Severity & loss-cost trend decomposition

Loss costs (pure premium per exposure) move through two distinct doors:

```
  pure-premium trend = (1 + frequency trend) · (1 + severity trend) − 1
```

Separating them matters: a rising loss cost driven by **severity** (bigger
claims — repair-cost inflation, medical, nuclear verdicts / social inflation) is a
different risk than one driven by **frequency** (more claims — exposure growth,
weather, distracted driving). They call for different pricing, reserving, and
mitigation responses. This skill fits the trend and does the decomposition; the
fitting method and trend-period mechanics are in [reference.md](reference.md).

## The method

Fit a **log-linear (exponential) trend** by OLS on the log of the series:

```
  ln(value_t) = a + b · t        (t in years from the first observation)
  annualized trend = exp(b) − 1
```

Reported alongside: **R²** (on the log scale — how cleanly exponential the series
is), and the **latest point vs the fitted trend** (a quick "running hot/cold"
read — is the most recent print above or below the established trend?).

Verified self-test: a synthetic 6%/yr series (`value = 100·1.06^t`) recovers
annual trend **6.00%**, R² **1.00**. Decomposition self-test: frequency −2%,
severity +8% → pure-premium trend **+5.84%** (≈ 0.98·1.08 − 1).

## Run it

```bash
# warehouse: list available severity indices, then trend one
python3 .claude/skills/severity-trend-decomposition/scripts/severity_trend.py --list
python3 .../severity_trend.py --db data/state.db --index-name blended_severity

# ad-hoc series on stdin
echo '{"series":[{"date":"2022-01-01","value":100},{"date":"2023-01-01","value":107},
                 {"date":"2024-01-01","value":119}]}' | python3 .../severity_trend.py --stdin

# decomposition from two series
echo '{"frequency":[...],"severity":[...]}' | python3 .../severity_trend.py --stdin
```

## On the warehouse

- **`severity_index`** — the blended loss-cost tape and its components
  (`category` ∈ used_vehicle, parts, labor, medical, blended), each with
  `value` and `zscore_12m`. `--index-name blended_severity` is the headline;
  trend each category to see *what* is driving the blend.
- The skill reports the latest `zscore_12m` from the warehouse — when it's hot
  (≥ ~2σ) the digest's `inflation_keyword_boost` already lifts severity-named
  items, so an elevated tape both *raises scores* and *confirms* a real
  loss-cost regime (cross-check via the `agent-server` tools).
- **FRED series** also live in `items` (source='fred') as the cost-driver
  anomalies; pull a level series there if `severity_index` is sparse.
- **Frequency** isn't directly in a single table — derive it from claim-count
  exhibits (EDGAR/statutory) or proxy it; then decompose against the severity
  tape. If you only have severity, say so and report severity trend alone.

## Feeds the rest of the pipeline

- The decomposed **loss-cost trend → `--trend-annual`** in the
  `ratemaking-indication` skill (project losses to the future period).
- Severity trend is the quantitative spine of the **liability chain** the Analyst
  watches: TPLF / nuclear verdicts → social inflation → **severity trend** →
  reserve adequacy. Rising severity with flat frequency on long-tail lines is the
  classic social-inflation fingerprint — corroborate with `litigation_pressure`
  and `disclosure_sentiment`.

## Discipline

- **Decompose before concluding.** "Loss costs up 9%" means little; "frequency
  flat, severity +9%" points straight at inflation/litigation, "+9% all
  frequency" points at exposure/weather. Always split when the data allows.
- **Check R² and fit window.** A low R² means the series isn't cleanly
  exponential — a single trend rate is misleading; consider a recent-years fit or
  a piecewise read. State the window.
- **Mind the units & seasonality.** Index level vs YoY%, monthly vs annual,
  seasonally adjusted or not — be explicit; the fit assumes a consistent series.
- **Trend ≠ level.** A high trend on a currently-low level is different from a low
  trend on an already-elevated level; report both the rate and the latest-vs-trend
  gap.
