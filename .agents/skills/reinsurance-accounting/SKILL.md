---
name: reinsurance-accounting
description: >-
  Ceded-reinsurance accounting for a P&C insurer — the gross→ceded→net premium and
  loss rollup, ceded/retention ratios, reinsurance recoverables as a balance-sheet
  asset and recoverable-to-surplus leverage, and the risk-transfer 10-10 rule /
  Expected Reinsurer Deficit test that decides reinsurance vs deposit accounting. Use
  when a question involves ceded vs net figures, how reinsurance moves the combined
  ratio, reinsurance recoverables / credit risk / Schedule F, ceding commission,
  quota-share vs excess-of-loss financial impact, or whether a treaty transfers
  enough risk to be accounted for as reinsurance.
---

# Ceded-reinsurance accounting

Reinsurance restates almost every line of an insurer's financials: premium, losses,
the balance-sheet recoverable asset, and the combined ratio all move when risk is
ceded. This skill does the **cession rollup** (gross→ceded→net, ratios, recoverable
leverage) and the **risk-transfer test** (10-10 + ERD) that determines whether a
contract even *qualifies* for reinsurance accounting. The helper does the arithmetic;
you supply the structural judgment.

Full theory — quota share vs XoL mechanics, the combined-ratio effect, retroactive vs
prospective, finite-reinsurance red flags, Schedule F — is in
[reference.md](reference.md).

## Where the data lives

- **`insurer_xbrl_facts`** — `dataset='reinsurance'` (`premiums_ceded`,
  `ceded_premiums_earned`, `recoverable_unpaid`) and `dataset='premiums'`
  (`premiums_written_net`, `premiums_earned_net`). Gross = net + ceded.
- **`statutory_facts`** — `dataset='surplus'` for the recoverable-leverage denominator.
- Treaty-level cash flows for the **risk-transfer test** are *not* in the warehouse —
  you supply the premium, brokerage and a loss-scenario distribution from the contract.

## Procedure

1. **Cession rollup** (default mode):
   ```bash
   python3 .Codex/skills/reinsurance-accounting/scripts/reinsurance_accounting.py \
       --db data/state.db --insurer TRV          # pulls ceded/net premium + recoverables
   # or fully explicit:
   python3 …/reinsurance_accounting.py --gross-written 5000 --ceded-written 1000 \
       --gross-earned 4800 --ceded-earned 960 --gross-incurred 3200 \
       --ceded-incurred 700 --recoverables 1800 --surplus 6000
   ```
   Read: cession ratio & net retention; **gross vs net vs ceded loss ratio** (ceded LR
   > gross LR ⇒ the reinsurer's slice ran hotter than the book ⇒ reinsurance was
   *favorable* to the cedant this period); **recoverable leverage** = recoverables ÷
   surplus (credit-risk concentration).

2. **Risk-transfer test** (`--mode risk_transfer`):
   ```bash
   python3 …/reinsurance_accounting.py --mode risk_transfer --premium 100 \
       --brokerage 5 --scenarios "0.70:0,0.15:50,0.10:150,0.04:300,0.01:600"
   ```
   Returns the **10-10 rule** (≥10% probability the reinsurer takes a ≥10%-of-premium
   loss) and the **ERD** (probability-weighted reinsurer deficit ÷ premium; threshold
   ~1%). Either passing ⇒ reinsurance accounting; both failing ⇒ **deposit
   accounting** (the contract is financing, not risk transfer). `--demo` for the
   worked example.

## Interpreting the result (judgment, not arithmetic)

- **Net is what the carrier keeps; gross is what it underwrote.** A low combined ratio
  on a heavily ceded book can mask weak gross underwriting — always note the cession
  ratio alongside the net combined ratio, and compare carriers on the same basis.
- **Ceded LR vs gross LR is the "did reinsurance pay off" read.** Ceded LR > gross LR
  means the reinsurer absorbed a disproportionate share (e.g. it took the cat layer in
  a cat year) — good for the cedant *that year*, but the reinsurer reprices next
  renewal. One year is not a trend.
- **Recoverables are an asset you may not collect.** Leverage > 100% of surplus is a
  red flag; the statutory **provision for reinsurance** (Schedule F) penalizes
  unauthorized/overdue balances and flows straight to surplus — connect to
  [[statutory-gaap-bridge]].
- **Retroactive ≠ prospective.** Retroactive reinsurance (covering *past* events, e.g.
  an adverse-development cover / LPT) defers the gain to a special surplus account
  rather than booking it to income — a carrier "fixing" a reserve hole with an ADC is
  not the same as earning it. Flag the distinction.
- **A treaty that fails 10-10 and ERD is finite/financial reinsurance** — surplus
  relief dressed as risk transfer. The metrics are exactly how an auditor (and the
  AIG-Gen Re-era enforcement) draw the line.

## Output discipline

Lead with net vs gross and the one ratio that answers the question. E.g. *"TRV cedes
20% of premium; net loss ratio 65.1% vs gross 66.7% — the ceded book ran at 72.9%, so
reinsurance was modestly favorable this year. Recoverables $1.8B = 30% of surplus,
not yet a credit concern."* For a treaty: *"ERD 18.8%, 10-10 passes at 15% — clear
risk transfer, reinsurance accounting is correct."*
