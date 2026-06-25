# Credibility reference — derivations, complements, and choosing a method

Backing for [SKILL.md](SKILL.md).

## 1. Classical credibility, derived

Limited-fluctuation credibility wants the observed estimate to be within ±k of its
mean with probability p. For Poisson claim counts with mean n, the normal
approximation gives full credibility when n ≥ n_full = (z_{(1+p)/2} / k)², where
z is the standard-normal quantile. Common standards:

| p (prob) | k (tolerance) | z | n_full (claims) |
|---|---|---|---|
| 90% | ±5% | 1.645 | 1,082 |
| 95% | ±5% | 1.960 | 1,537 |
| 90% | ±2.5% | 1.645 | 4,329 |

For **partial** credibility the square-root rule Z = √(n/n_full) makes the
*variance contributed by the observation* scale to the full-credibility variance.
If you are crediting aggregate losses (not counts), inflate n_full for severity
variance: n_full,$ = n_full × (1 + CV_severity²).

## 2. Bühlmann credibility, derived

Bühlmann credibility is the *linear least-squares* approximation to the Bayesian
estimate — the best estimator of the form a + b·(observed). Decompose total
variance of an observation into:
- **EPV** = E[ Var(X | risk) ] — the average process variance *within* a risk.
- **VHM** = Var( E[X | risk] ) — the variance of the true means *across* risks.

Then Z = N/(N+K) with K = EPV/VHM exactly minimizes expected squared error. The
intuition: K is the number of observations at which the data earns half
credibility (Z = 0.5). **Bühlmann-Straub** generalizes to unequal exposures per
cell (weight each by exposure m_ij) — the version you need for real triangles or
unbalanced state data; the balanced estimator in the helper is the special case
of equal group sizes.

Empirical Bayes estimates EPV/VHM from the data itself (the helper's `groups`
mode). It is unbiased but can yield VHM ≤ 0 on small samples — interpret that as
"no detectable between-risk signal," Z → 0.

## 3. Choosing the complement of credibility

The Z is only half the job; the complement carries 1 − Z of the weight. Good
complements (Boor's criteria: accurate, unbiased, statistically independent of the
data, available, logical, easy to compute):
- **Loss costs of a larger related group** (the countrywide book for a state).
- **Prior indication / prior estimate**, trended forward.
- **Harwayne's method** (adjust a related book for distributional differences).
- **Trended present rates** (for rate indications).
- **Rate change of the larger group** applied to the present rate.

A complement that shares the data's noise (e.g. last year's *same* thin cell)
defeats the purpose.

## 4. Where this sits relative to the other skills

- It **stabilizes** the outputs of `reserving-chain-ladder` (green-year IBNR),
  `ratemaking-indication` (thin experience-period indication), and `glm-pricing`
  (sparse cells — GLMs handle this via the model, but one-way relativities on thin
  levels still want credibility).
- The Bühlmann mixed-model view is the bridge to GLMM / hierarchical credibility;
  for a one-off blend the closed forms here are enough.

## 5. Pitfalls

- Using a full-credibility standard for *counts* on a *dollar* (aggregate) loss
  ratio without the CV-of-severity inflation.
- Treating Z as the answer — the complement choice usually moves the estimate more
  than the precise Z does.
- Empirical VHM < 0 quietly clamped to "full complement weight" — the helper
  surfaces this; don't hide it.
- Crediting and trending in the wrong order, or crediting an already-credibility-
  weighted number twice.
