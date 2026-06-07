# GLM pricing reference — IRLS, the exponential family, and Tweedie

Backing for [SKILL.md](SKILL.md).

## 1. The exponential family and why these three

A GLM models `g(μ) = Xβ` where the response is in the exponential family with a
mean-variance link `Var(Y) = φ·V(μ)`. The variance function encodes the loss
process:
- **Poisson (V = μ):** counts; variance grows with the mean — right for claim
  *frequency*. With a log link and `log(exposure)` offset, it is a rate model.
- **Gamma (V = μ²):** positive, right-skewed, constant CV — right for claim
  *severity*. Weight each observation by its claim count so a class average built
  on more claims counts more.
- **Tweedie (V = μ^p, 1<p<2):** a Poisson-sum-of-Gammas — a point mass at zero
  (policies with no claim) plus a continuous positive part. The natural single
  model for *pure premium*; p→1 approaches Poisson, p→2 approaches Gamma. Weight
  by exposure.

The **log link** is chosen for pricing because it makes effects multiplicative,
matching how rate tables and rating algorithms actually combine factors.

## 2. IRLS (what the helper does)

Maximum likelihood for a GLM solves the score equations by iteratively reweighted
least squares. Each iteration is a weighted regression:

```
  η = Xβ + offset ;   μ = exp(η)            (log link)
  working weight   w_i = prior_w_i · (dμ/dη)² / V(μ)        (dμ/dη = μ for log link)
                       = prior_w_i · μ² / V(μ)
                       = prior_w · μ        (Poisson)
                       = prior_w            (Gamma — constant!)
                       = prior_w · μ^{2−p}  (Tweedie)
  working response z_i = (η_i − offset_i) + (y_i − μ_i)/μ_i
  solve (XᵀWX) β = XᵀWz ;   repeat to convergence
```

Convergence is quadratic near the optimum; the helper iterates to a 1e-10 coef
change. The dispersion φ doesn't affect the point estimates of β (it cancels in
the weights up to a constant), which is why relativities are recoverable without
estimating φ — but φ *does* matter for standard errors, which this scope omits.

## 3. The one-way recovery proof (the test)

For a single categorical factor with a log link and the canonical/quasi weights,
the MLE sets fitted = observed *group* rate (Poisson, exposure offset) or *group*
mean (Gamma). Hence `exp(β0)` = baseline group rate/mean and `exp(β_level)` =
ratio of that level's rate/mean to baseline. This is the exact, checkable property
the helper is validated on — if a future change broke IRLS, this test would catch
it.

## 4. Frequency–severity vs Tweedie

- **Two-part (freq × severity):** fit Poisson frequency and Gamma severity
  separately, multiply the relativities. Diagnostic — you see whether a factor
  drives frequency (e.g. territory, exposure to accidents) or severity (e.g.
  vehicle value, injury type). Standard in personal auto.
- **Tweedie pure premium:** one model on loss cost per exposure, handles the mass
  of zero-claim policies natively. Compact and avoids combining two models'
  uncertainty, but hides the freq/sev split. Choose p by profile likelihood
  (~1.5 is a common default for auto/home loss cost).

## 5. From relativities to a rate table

1. Fit relativities (this skill) — the *differentials*.
2. Re-balance to be revenue-neutral at current volume (offsetting base-rate
   change) so the relativity update alone doesn't change total premium.
3. Apply the **overall indicated change** (the `ratemaking-indication` skill) on
   top — level and differentials are separate decisions.
4. Apply selection judgment, capping, and credibility (`credibility-weighting`)
   to thin cells.

## 6. Pitfalls

- **Exposure as a predictor instead of an offset** → wrong, double-counts volume.
- **One-way tables instead of a GLM** → correlated factors get double-credited
  (the original motivation for GLMs in ratemaking).
- **Aliasing / perfect collinearity** → singular design matrix (the solver
  raises); drop or combine the redundant factor.
- **Over-parameterization on rare levels** → unstable relativities; group sparse
  levels or credibility-weight.
- **In-sample fit ≠ predictive lift** → validate on holdout; compare deviance /
  Gini / lift, not just fitted error.
- **No standard errors here** → don't read precision into a single fit; this tool
  estimates relativities, it doesn't do inference.
