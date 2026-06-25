---
name: glm-pricing
description: >-
  Generalized linear models for P&C pricing — Poisson frequency, Gamma severity,
  Tweedie pure premium, log link, with exposure offset → multiplicative rating
  relativities. Use when a question involves rating factors/variables, class or
  territory relativities, frequency/severity modeling, a multiplicative rating
  plan, exposure offsets, or interpreting GLM coefficients as relativities.
---

# GLM pricing — rating relativities

Modern P&C class pricing is a **multiplicative rating plan** fit with a
generalized linear model. A log link turns additive coefficients into
multiplicative **relativities**:

```
  log(μ_i) = offset_i + β0 + Σ_j β_j · x_ij
  rate_i   = exp(offset_i) · exp(β0) · Π_j relativity_j ,   relativity_j = exp(β_j)
```

So `exp(β0)` is the **base rate** (all factors at their baseline level) and each
`exp(β_j)` is the multiplier for being in level j instead of baseline. This is
exactly how a rate table is built. The helper fits the GLM by IRLS in pure Python;
the math (IRLS, the exponential family, deviance, the two-part vs Tweedie choice)
is in [reference.md](reference.md).

## The three families (all log link)

| Target | Family | Variance V(μ) | Weight / offset | Gives |
|---|---|---|---|---|
| claim **count** | Poisson | μ | offset = log(exposure) | **frequency** relativities |
| claim **severity** | Gamma | μ² | weight = claim count | **severity** relativities |
| **pure premium** | Tweedie (1<p<2) | μ^p | weight = exposure | **loss-cost** relativities |

The classic approach models **frequency × severity** separately (two GLMs) and
multiplies; **Tweedie** models pure premium directly in one GLM. Frequency/severity
is more diagnostic (you see *which* driver moves); Tweedie is more compact.

**Exposure belongs in the offset, not as a predictor:** with a log link, putting
`log(exposure)` in the offset constrains its coefficient to 1, i.e. models a
*rate per exposure* — the actuarially correct treatment.

## Verification property (why you can trust the fit)

For a **single categorical predictor**, a Poisson/Gamma log-link GLM exactly
reproduces the observed group rates/means. The helper is tested on this:

```
region A: exposure 100, count 10  (rate 0.10)   ← baseline
region B: exposure 200, count 40  (rate 0.20)
region C: exposure  50, count 10  (rate 0.20)
→ baseline_rate 0.10, relativity region=B ×2.00, region=C ×2.00   ✓
```

Multi-factor models then estimate each factor's effect *controlling for the
others* (the whole point of a GLM over one-way tables, which double-count
correlated exposure).

## Run it

```bash
echo '{"family":"poisson","rows":[
  {"exposure":1000,"count":80,"factors":{"territory":"urban","age":"young"}},
  {"exposure":3000,"count":120,"factors":{"territory":"rural","age":"adult"}},
  ... ]}' | python3 .Codex/skills/glm-pricing/scripts/glm_pricing.py --format json
```

- Baseline level per factor = first alphabetically (deterministic); relativities
  are read off baseline.
- `family:"gamma"` rows use `"severity"` (+ optional `"count"` weight);
  `family:"tweedie"` rows use `"pure_premium"` (+ `"exposure"` weight) and an
  optional `"var_power"` p (default 1.5).
- Pass a file with `--data file.json` instead of stdin.

## Using it on the warehouse

The warehouse has no policy-level exposure data, so GLM pricing here is a **method
tool** you feed with a small extracted dataset:
- Build a frequency/severity dataset from a carrier's statutory exhibits, a state
  DOI data call, or rate-filing support pulled from `serff` / EDGAR filing
  content, then fit relativities to *explain* or *sanity-check* a filed plan.
- Cross-read with `ratemaking-indication`: GLMs set the **differentials**
  (relativities, revenue-neutral); the indication sets the **overall level**.
- Thin cells → corroborate with `credibility-weighting` before trusting a lone
  relativity.

## Interpreting & cautions

- **Relativities are multiplicative and off a baseline** — `×1.5` means 50% more
  than the baseline level, and they compound across factors. Always state the
  baseline.
- **Significance & sample:** a relativity from a sparse cell is unstable; the
  helper reports coefficients but not standard errors — treat large swings on thin
  cells skeptically and credibility-weight.
- **Correlated predictors:** GLMs handle moderate correlation but not aliasing
  (perfectly collinear factors → the solver raises "singular design matrix").
- **Validate out of sample / by holdout** before believing a plan; in-sample fit
  always improves with parameters. Watch for over-fit on rare levels.
- This is a deterministic IRLS for relativity estimation, not a substitute for a
  full modeling stack (no SEs, splines, interactions, or regularization) — for
  production modeling, move to `glum` / `statsmodels` / `R`. The relativities here
  match those tools for the clean cases this scope covers.
