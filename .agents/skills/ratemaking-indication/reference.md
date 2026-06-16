# Ratemaking reference — on-leveling, trend, and method equivalence

Backing for [SKILL.md](SKILL.md). Read for the mechanics behind the inputs.

## 1. Why on-level and trend are non-negotiable

You are pricing the *future* policy period from *past* experience. Two distortions
must be removed first:

- **On-leveling premium** restates historical earned premium to current-rate-level
  (CRL) — as if today's rates had always been in force. Without it, the loss
  ratio compares future losses to stale premium and you double-count past rate
  changes. The **parallelogram method** (geometric, assuming uniform writing) or
  the **extension-of-exposures method** (re-rate each policy, exact) produces the
  on-level factor. Feed it as `--on-level`.
- **Trending** projects past losses (and, for pure premium, exposures) to the
  midpoint of the future policy period. Loss trend = frequency trend × severity
  trend (see the `severity-trend-decomposition` skill). The trend period runs
  from the average accident date of the experience to the average accident date
  of the future period — often 1.5–2.5 years for annual policies. Feed
  `--trend-annual` and `--trend-years`.
- **Loss development** brings immature accident periods to ultimate (chain-ladder;
  see the `reserving-chain-ladder` skill). Feed `--development`.

Order: develop to ultimate, then trend to the future period; on-level the premium
separately. The script applies `losses × development × trend` and
`premium × on_level`.

## 2. The permissible loss ratio

`PLR = 1 − variable-expense ratio − target UW profit` is the fraction of each
premium dollar available to pay losses + LAE + fixed expenses. The indication
asks whether projected L&LAE + fixed expense fits inside the PLR. Expense
provisions usually come from a **premium-weighted expense exhibit**; the
fixed/variable split matters because only variable expenses scale with the rate
change itself (which is why fixed expense sits in the numerator, not the
denominator).

## 3. Why the two methods agree

For the same projected losses and expense structure, the loss-ratio and
pure-premium methods are algebraically identical — one is the other divided by
exposures. Use **pure premium** when exposures are well-defined and homogeneous
(personal auto by class); use **loss ratio** when premium is clean but exposures
are mixed or unavailable (most commercial books). They diverge only if you feed
them inconsistent premium vs exposure bases — a useful cross-check.

## 4. Beyond the overall indication

- **Credibility:** a thin experience period gets a credibility-weighted
  indication: `Z × indicated + (1 − Z) × complement` (trend, a related book, or
  the prior filing). Use the `credibility-weighting` skill.
- **Class / territory relativities:** the *overall* indication sets the level;
  GLMs (the `glm-pricing` skill) set the *differentials*. Re-balance relativities
  to be revenue-neutral, then apply the overall change on top.
- **Capping & dislocation:** filed plans cap individual policy swings; the filed
  average then differs from the indicated average. This is a primary reason
  indicated ≠ filed even absent regulation.

## 5. Pitfalls

- Comparing a loss ratio computed on *earned* premium to losses on an *accident*
  basis without aligning periods.
- Forgetting LAE (ALAE + ULAE) — a pure-loss ratio understates the indication.
- Using calendar-year losses (contaminated by prior-year reserve development)
  instead of accident-year ultimates.
- Letting a single large loss or cat year set the trend/level without
  capping or loading catastrophes separately.
- Reading a regulator-suppressed filed change as the carrier's true loss pick —
  the indication is the truer read of adequacy.
