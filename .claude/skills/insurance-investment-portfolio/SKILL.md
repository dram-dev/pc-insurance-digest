---
name: insurance-investment-portfolio
description: >-
  The asset side of a P&C insurer — float and cost of float, net investment income,
  book yield vs new-money yield (the reinvestment dynamic), investment leverage, and
  asset-liability management (duration gap + the AOCI rate shock to GAAP equity). Use
  when a question involves an insurer's investment portfolio, net investment income,
  book yield, float, cost of float, reinvestment, duration matching / ALM, the
  unrealized AFS bond gain/loss in AOCI, rate sensitivity of book value, or how the
  investment side drives ROE. An insurer's portfolio is funded by float and
  duration-constrained — it is not a generic bond book.
---

# P&C insurer investment portfolio & ALM

An insurer's investments are funded by **policyholder float** and constrained to
**match the liability duration**, and their mark-to-market runs through **AOCI** into
GAAP equity. So the right reads are insurance-specific: cost of float, book vs
new-money yield, investment leverage, the duration gap, and the AOCI rate shock — not
a generic Sharpe/return view. The helper computes all four; you supply the rate and
duration judgment.

Full theory — the float identity, the reinvestment dynamic, duration/convexity, the
economic-vs-GAAP surplus distinction, credit quality — is in [reference.md](reference.md).

## Where the data lives

- **`insurer_xbrl_facts`** — `investment_income.net_investment_income` (NII),
  `investment_portfolio.investments_fair_value` (invested assets) and
  `.afs_debt_securities` (the AFS bond book), `aoci.oci_net`, plus the float inputs:
  `unpaid_claims.liability_net` (reserves), `premiums.unearned_premiums`,
  `reinsurance.recoverable_unpaid`, `dac.dac_balance`, `equity.common_equity`.
- **Durations, new-money yield and the rate shock are NOT ingested** — supply asset &
  liability duration (from the 10-K investment footnote / your estimate), the
  new-money yield, and the bp shock.

## Procedure

```bash
python3 .claude/skills/insurance-investment-portfolio/scripts/investment_portfolio.py \
    --db data/state.db --insurer CB \
    --asset-duration 4.5 --liability-duration 3.0 --new-money-yield 0.055 --rate-shock 0.01
# or fully explicit — see the script header / --demo.
```

Read the four blocks:
- **Float** = loss & LAE reserves + unearned premium − recoverables − DAC − agents'
  balances. **Cost of float = −underwriting profit / float** (negative = the carrier is
  *paid* to hold the money — Buffett's framing).
- **Book yield = NII / invested assets** vs **new-money yield** → reinvestment drift
  (the tailwind/headwind as the book rolls over at current rates).
- **Investment leverage = invested assets / equity**; NII adds `book yield × leverage`
  to pretax ROE — the asset side's contribution to the return.
- **ALM**: duration gap = asset − liability duration; **AOCI hit = −Dₐ × ΔY × bond MV**
  (the unrealized P&L into GAAP equity), and the economic surplus change if liabilities
  revalue (they don't in GAAP — reserves are nominal).

`--demo` runs the verified worked example; `--stdin` takes JSON.

## Interpreting the result (judgment, not arithmetic)

- **Negative cost of float is the whole game.** A carrier with an underwriting profit is
  *paid* to hold investable float — that's leverage on the investment return at a
  negative borrowing cost. A carrier with an underwriting *loss* has a positive cost of
  float and must out-earn it on the assets just to break even. This is the single most
  important read of an insurer's business model.
- **Book yield lags new-money yield** — the portfolio reprices slowly as bonds mature.
  After the 2022 rate jump, book yields are still climbing toward new-money for years
  (a multi-year NII tailwind); when rates fall, the reverse. The reinvestment drift
  tells you the *direction* of NII independent of any new capital.
- **The duration gap is the rate bet.** P&C liabilities are short-to-medium (claims pay
  out over a few years); cat/short-tail books carry short reserve duration, long-tail
  (WC, GL, umbrella) carry long. Matching protects economic surplus; a positive gap
  (longer assets) means rising rates hurt. Read the gap *against the line of business*.
- **AOCI ≠ economic loss.** A +100bps shock can knock 9% off GAAP equity through the
  AFS mark — but if the carrier holds to maturity and the liabilities are shorter than
  marked, the *economic* hit is smaller (reserves aren't marked up in GAAP). This is the
  2022 story: huge AOCI book-value declines, far smaller economic damage. Connect to
  [[statutory-gaap-bridge]] (statutory surplus ignores the AOCI mark entirely).
- **NII is the stable earnings leg.** When underwriting is cyclical, recurring NII is
  the floor under ROE — and rising NII can mask deteriorating underwriting. Separate
  the two (see [[combined-ratio-bridge]]).

## Output discipline

Lead with cost of float and the NII trajectory, then the rate risk. E.g. *"CB holds
$30.5B of float at a −2.6% cost (underwriting profit funds it — it's paid to invest).
Book yield 4.0% vs 5.5% new money → a ~150bp multi-year NII tailwind as bonds roll.
Investment leverage 2.4× means that yield adds ~9.6 pts to pretax ROE. Rate risk: a
+100bp shock is a $2.25B AOCI hit (−9% of GAAP equity) but only ~$1.3B economic given
the +1.5y duration gap — book-value optics overstate the damage."*
