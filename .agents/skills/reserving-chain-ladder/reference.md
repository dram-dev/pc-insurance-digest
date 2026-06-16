# Chain-ladder reference — theory, worked example, and the next methods

Deeper backing for [SKILL.md](SKILL.md). Read this when you need the mechanics,
factor-selection judgment, uncertainty, or when chain-ladder is the wrong tool.

## 1. The model in one paragraph

A cumulative loss triangle has accident years (AY) down the rows and development
periods (dev) across the columns; cell `C(i, j)` is cumulative paid or incurred
loss for AY *i* at development age *j*. Only the upper-left half is observed (a
younger AY has had fewer periods to develop). Chain-ladder assumes development is
*proportional* — the ratio from one age to the next is roughly constant across
accident years — so we estimate one **age-to-age factor** per dev step from the
observed history and use it to fill the lower-right half out to ultimate.

## 2. Worked example (the script's self-test)

Cumulative triangle:

| AY \ dev | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| 2019 | 1,000 | 1,500 | 1,750 | 1,800 |
| 2020 | 1,200 | 1,800 | 2,100 | |
| 2021 | 1,100 | 1,650 | | |
| 2022 | 1,300 | | | |

**Volume-weighted age-to-age factors** (Σ next ÷ Σ current, over AYs with both):
- `f(0→1) = (1500+1800+1650) / (1000+1200+1100) = 4950/3300 = 1.5000`
- `f(1→2) = (1750+2100) / (1500+1800) = 3850/3300 = 1.16667`
- `f(2→3) = 1800/1750 = 1.02857`  ← rests on **one** ratio → low credibility

**CDF to ultimate** (product of remaining factors × tail; tail = 1.0 here):
- `CDF(dev 3) = 1.0`
- `CDF(dev 2) = 1.02857`
- `CDF(dev 1) = 1.16667 × 1.02857 = 1.2000`
- `CDF(dev 0) = 1.5 × 1.2 = 1.8000`

**Develop each AY** from its latest diagonal cell (latest × CDF):

| AY | latest | CDF | ultimate | IBNR |
|---|---|---|---|---|
| 2019 | 1,800 | 1.0000 | 1,800 | 0 |
| 2020 | 2,100 | 1.0286 | 2,160 | 60 |
| 2021 | 1,650 | 1.2000 | 1,980 | 330 |
| 2022 | 1,300 | 1.8000 | 2,340 | 1,040 |

Totals: latest 6,850 · ultimate 8,280 · **IBNR 1,430** (20.9% of latest). Note
how the youngest AY (2022) is 73% of the IBNR and leans entirely on `CDF(0)=1.8`,
which compounds the weakest factors — hence the credibility discipline.

## 3. Factor selection (the main judgment call)

The script reports three views per dev step; choosing among them is actuarial work:
- **Volume-weighted (all-year)** — the default; weights big AYs more. Robust when
  AY sizes vary and mix is stable.
- **Simple average** — equal weight per AY; better when a recent AY's *pattern*
  (not size) is what should dominate.
- **Latest-N / excluding outliers** — when a law change, a large loss, or a
  case-reserving change makes old years unrepresentative, select on recent years
  only. (Re-run with a trimmed triangle to see the effect.)

Decision aids in the output: a **low CV** means the link ratios agree (any
average is fine); a **high CV** or **low n** means the factor is fragile — widen
your uncertainty and consider Bornhuetter-Ferguson.

## 4. Tail factor

The triangle only sees development up to its width. Long-tail lines (general
liability, workers comp, D&O, umbrella, asbestos/PFAS) keep developing for years
beyond it. A `--tail` > 1.0 extends the oldest age to true ultimate. Common
approaches: fit a curve (inverse-power / exponential) to the observed factors and
extrapolate; use an industry/benchmark tail; or borrow the insurer's disclosed
selection. Omitting a tail on a long-tail line **understates ultimate and IBNR** —
always state the assumption.

## 5. Uncertainty (Mack, briefly)

Chain-ladder gives a point estimate; the **Mack (1993)** model adds a standard
error around it without assuming a distribution, from the dispersion of link
ratios per dev step and the volume in each AY. The CV the script reports is a
quick proxy for the per-step volatility Mack formalizes. For a full standard
error / prediction interval, develop with `chainladder-python`
(`casact/chainladder-python`) — the documented upgrade path noted in
`src/digest/reserving.py` — or a bootstrap (resample residuals, re-develop,
read the distribution of ultimates). Use these when the *range* matters (capital,
reserve-risk), not just the central pick.

## 6. Bornhuetter-Ferguson — when chain-ladder over-reacts

Pure chain-ladder multiplies a *thin* latest diagonal by a *large* CDF, so a
single noisy cell in a green AY swings ultimate wildly. **Bornhuetter-Ferguson**
blends chain-ladder with an a-priori expected loss (e.g. premium × expected loss
ratio):

```
Ultimate_BF = Actual_to_date + Expected × (1 − 1/CDF)
```

i.e. trust actuals for the *developed* portion and the a-priori for the
*undeveloped* portion. Prefer BF (or Cape Cod, which derives the a-priori from
the triangle itself) for the youngest 1–2 accident years and whenever
`CDF` is large and `n` is small. Chain-ladder is fine for mature AYs.

## 7. Pitfalls checklist

- **Paid vs incurred IBNR are not comparable** (see SKILL.md). Develop and label
  them separately.
- **Negative / sub-1.0 factors**: salvage/subrogation recoveries or incurred
  releases; legitimate but verify it isn't a data error. The script flags these.
- **Interior gaps** in an AY break the proportional-development chain — the script
  flags the AY; don't trust its developed cell.
- **Calendar-year (diagonal) effects** — inflation spikes, a tort-reform law,
  COVID — hit *all* AYs at one diagonal and violate the constant-factor
  assumption. Chain-ladder can't see them; corroborate with `severity_index`,
  `disclosure_sentiment`, and the news flow.
- **Mix / volume shifts** make old AYs unrepresentative → reconsider factor
  selection.
- **One large loss** in an AY inflates its factors → consider capping or
  ex-large-loss development.
- **Green-year over-reaction** → use BF (§6).

## 8. How this maps to the warehouse

- The pipeline's `digest reserving` runs exactly this volume-weighted method
  (`src/digest/reserving.py::chain_ladder`) and stores totals + the prior-period
  comparison in `reserving_signals` (`direction`, `deterioration_pct`).
- `adverse` development with a positive `deterioration_pct` is the signal that
  matters; it is also what `reserve_deterioration_boost` is built to amplify once
  wired into the leaderboard.
- This skill adds the **per-AY decomposition and credibility diagnostics** the
  roll-up hides — use it to explain *which accident years and which factors* drive
  a stored adverse/favorable signal, not just that one exists.
