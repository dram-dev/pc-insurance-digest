---
name: credibility-weighting
description: >-
  Credibility weighting for thin/volatile estimates — classical (limited
  fluctuation) and Bühlmann (least-squares / empirical Bayes). Use when a
  question involves how much to trust a small sample, blending observed
  experience with a complement, full-credibility standards, partial credibility,
  experience rating, or stabilizing a noisy loss ratio / frequency / severity
  before it drives a decision.
---

# Credibility weighting

Insurance data is thin and noisy: a single state, class, or accident year may
have too few claims to trust on its own. Credibility answers *how much weight* to
put on the observed data versus a more stable **complement of credibility** (a
broader book, the prior estimate, a trend, an a-priori loss ratio):

```
  estimate = Z · observed + (1 − Z) · complement,     0 ≤ Z ≤ 1
```

The whole craft is (a) picking Z honestly and (b) picking a *good* complement.
This skill gives both classical and Bühlmann Z; deeper derivations and complement
selection are in [reference.md](reference.md). All formulas below are verified in
the helper's self-tests.

## Classical (limited-fluctuation) credibility

"Give full credibility once the data is stable enough that random fluctuation is
within ±k with probability p." For Poisson claim counts:

```
  full-credibility standard   n_full = ( z_{(1+p)/2} / k )²   claims
  partial credibility         Z = min( 1, sqrt( n / n_full ) )
```

The canonical standard is **p = 90%, k = 5% → n_full ≈ 1,082 claims**
(z = 1.645, (1.645/0.05)² = 1,082). The square-root rule scales Z down for
smaller samples. Example: n = 271 claims → Z = √(271/1082) = **0.50**; with an
observed loss ratio 0.80 and complement 0.65, the credibility-weighted loss ratio
is 0.50·0.80 + 0.50·0.65 = **0.725**.

```bash
python3 .claude/skills/credibility-weighting/scripts/credibility.py \
  --mode classical --n 271 --p 0.9 --k 0.05 --observed 0.80 --complement 0.65
# override the standard directly with --full 1082, or change --p/--k
```

## Bühlmann (greatest-accuracy) credibility

The least-squares optimal credibility. Instead of a fixed standard, it weighs
*signal vs noise*:

```
  Z = N / (N + K),     K = EPV / VHM
    EPV = Expected Process Variance     (noise within a risk, period to period)
    VHM = Variance of Hypothetical Means (true signal between risks)
```

Low K (signal ≫ noise) → high credibility fast; high K (noise ≫ signal) → trust
the data slowly. Give K directly, or estimate it **empirically** (Bühlmann /
Bühlmann-Straub) from grouped data:

```
  EPV ≈ mean of within-group sample variances
  VHM ≈ variance of group means  −  EPV / N
  K = EPV / VHM,   Z = N / (N + K)
```

Worked example (groups A = [10,12], B = [20,18], so N = 2, r = 2):
EPV = 2, VHM = var([11,19]) − 2/2 = 32 − 1 = **31**, K = 2/31 = 0.0645,
Z = 2/2.0645 = **0.969** → group A credibility-weighted to 0.969·11 + 0.031·15 =
**11.13** (grand mean 15). High Z because the between-group spread dwarfs the
within-group noise.

```bash
echo '{"mode":"buhlmann","groups":[[10,12],[20,18]]}' \
  | python3 .../credibility.py --stdin
# or supply moments:  --mode buhlmann --n 5 --epv 10 --vhm 2   (→ K=5, Z=0.5)
```

If VHM ≤ 0 (groups indistinguishable beyond process noise) the helper returns
Z = 0 — i.e. trust the grand mean, not the group.

## Using it on the warehouse

- **Stabilize a per-state / per-source rate** before ranking on it. E.g. a
  source's average `signal_scores.score`, or a state's burden index from
  `burden_by_state`, computed on few items is noisy — credibility-weight it
  toward the all-source / national mean (n = item or claim count).
- **Reserving & ratemaking hand-off:** a green accident year's chain-ladder IBNR
  (from `reserving-chain-ladder`) or a thin experience period's rate indication
  (from `ratemaking-indication`) should be blended with a complement using Z
  before it drives a conclusion. This is the glue between those skills.
- **Litigation / verdict counts** in `litigation_pressure` for a single
  state×sector are low-n — weight toward the national roll-up.

## Discipline

- **Z is not optional for small n.** State the claim/observation count and the Z;
  never present a thin estimate as if fully credible.
- **A bad complement poisons a good Z.** The complement must be relevant and more
  stable than the data (broader book, prior, trend). Say what it is.
- **Classical vs Bühlmann:** classical is simple and regulator-familiar but
  assumes a full-credibility threshold; Bühlmann is optimal but needs EPV/VHM
  (or grouped data to estimate them). Prefer Bühlmann when you have the structure.
