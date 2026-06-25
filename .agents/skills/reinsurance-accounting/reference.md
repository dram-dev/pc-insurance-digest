# Ceded-reinsurance accounting — reference

Deeper backing for [SKILL.md](SKILL.md): treaty mechanics, the combined-ratio effect,
the risk-transfer test, and the worked examples the script's `--demo` reproduces.

## 1. Gross → ceded → net

Every income-statement and balance-sheet line has three views:
- **Gross (direct + assumed)** — everything the carrier underwrote.
- **Ceded** — the slice handed to reinsurers (premium out, losses recovered).
- **Net = gross − ceded** — what the carrier retains and reports as the headline.

```
net written  = gross written  − ceded written
net earned   = gross earned   − ceded earned
net incurred = gross incurred − ceded incurred (recoveries)
cession ratio   = ceded written / gross written
net retention   = net written  / gross written = 1 − cession ratio
```

Three loss ratios, three stories:
- **gross LR** = gross incurred / gross earned — underwriting quality before reinsurance.
- **net LR** = net incurred / net earned — what hits the P&L.
- **ceded LR** = ceded incurred / ceded earned — how the *reinsurer's* slice ran.

`ceded LR > gross LR` ⇒ the reinsurer absorbed more than its premium share (favorable
to the cedant this period). `ceded LR < gross LR` ⇒ the cedant ceded profitable
business (the reinsurer won) — common with proportional quota share at a soft point in
the cycle.

## 2. The combined-ratio effect (why ceding can help OR hurt the ratio)

Ceding lowers *both* the premium denominator and the loss numerator, so the net
combined ratio can move either way:
- **Quota share** cedes a flat % of premium and losses, and pays a **ceding
  commission** back to the cedant that offsets acquisition expense → it mostly affects
  the *expense* ratio and is roughly loss-ratio-neutral unless the ceding commission ≠
  the cedant's own acquisition cost. It is the classic **surplus-relief** tool (cede
  premium → free up the premium-to-surplus leverage).
- **Excess-of-loss (XoL)** cedes only losses above an attachment, for a fixed
  reinsurance premium → it *raises* the net loss ratio in quiet years (you paid premium
  for protection you didn't use) and *caps* it in bad years. Net of XoL, the combined
  ratio is smoother but averages slightly higher.

Connect to [[combined-ratio-bridge]]: always confirm a combined ratio is net-of-
reinsurance (it usually is) and on the same cession basis as the comparable.

## 3. Reinsurance recoverables, Schedule F, and credit risk

Ceded losses the reinsurer hasn't yet paid sit on the cedant's balance sheet as a
**reinsurance recoverable** asset — often the largest single asset for a long-tail
specialty writer. Two risks:
- **Credit / collectibility** — the reinsurer may dispute or fail. Statutory **Schedule
  F** classifies recoverables by authorized/unauthorized/certified and overdue status,
  and computes a **provision for reinsurance** that is charged directly to surplus
  (ties to [[statutory-gaap-bridge]]).
- **Concentration** — `recoverable leverage = recoverables / surplus`. > 1.0× means a
  reinsurer default could wipe out a year of capital; rating agencies haircut heavily
  above that.

## 4. Retroactive vs prospective (the gain-deferral trap)

- **Prospective** reinsurance covers *future* events — normal accounting, premium and
  losses through the income statement.
- **Retroactive** reinsurance covers *past* events already incurred — an adverse-
  development cover (ADC) or loss-portfolio transfer (LPT). Any day-one gain (paying
  less than the reserves transferred) is **deferred to a special surplus account** and
  amortized, *not* booked to income. A carrier that "solves" a reserve deficiency with
  an LPT has transferred the risk but cannot recognize the relief as earnings — read
  the move as a reserve-adequacy signal, not a profit. Connect to
  [[reserving-chain-ladder]] / disclosure tone.

## 5. The risk-transfer test (10-10 rule + ERD)

For a contract to use **reinsurance accounting** (vs **deposit accounting**, where it
is just financing on the balance sheet), it must transfer significant insurance risk.
Two standard screens, applied to the *reinsurer's* economics on a PV basis:

reinsurer result = premium − brokerage/expense − ceded losses (− reinstatements …)

- **10-10 rule** (rule of thumb): there is at least a **10% probability** of the
  reinsurer suffering a loss of at least **10% of premium**.
- **Expected Reinsurer Deficit (ERD)** (preferred, captures the tail 10-10 misses):
  `ERD = Σ pᵢ · max(−resultᵢ, 0) / premium`. Threshold ≈ **1%**.

Either passing ⇒ reinsurance accounting. Both failing ⇒ deposit accounting — the
hallmark of **finite / financial reinsurance** (surplus relief masquerading as risk
transfer; the AIG–Gen Re case). ERD is favored because a contract can fail 10-10 yet
have a fat, low-probability tail that genuinely transfers risk.

## 6. Worked examples (the script's `--demo`)

**Cession ($M):** gross written 5,000 / ceded 1,000 → net 4,000 (cession 20%,
retention 80%). Gross earned 4,800 / ceded 960 → net 3,840. Gross incurred 3,200 /
ceded 700 → net 2,500. Loss ratios: gross 66.7%, net 65.1%, **ceded 72.9%** ⇒
favorable to cedant. Recoverables 1,800 / surplus 6,000 = **30% leverage** (fine).

**Risk transfer:** premium 100, brokerage 5; scenarios (prob:ceded loss)
0.70:0, 0.15:50, 0.10:150, 0.04:300, 0.01:600. Reinsurer result = 95 − loss.
Deficits occur in the last three scenarios (55, 205, 505 = 55/205/505% of premium),
total probability **15% ≥ 10% ⇒ 10-10 PASS**. ERD = (.10·55 + .04·205 + .01·505)/100
= **18.75% ≫ 1% ⇒ PASS**. Clear risk transfer → reinsurance accounting. (E[reinsurer
result] = +54.5, so the *reinsurer* expects a profit — risk transfer is about the
*distribution*, not the mean.)

## 7. Pitfalls checklist

- **Incomplete premium understates risk transfer** — reinstatement premiums, profit
  commissions and sliding-scale features change the reinsurer's distribution; the
  script warns if E[reinsurer result] < 0, a tell that an inflow is missing.
- **Assumed reinsurance** (the carrier *as* reinsurer) is the mirror image — gross
  includes assumed; don't double-count with ceded.
- **Funds-withheld / modco** leave assets (and investment income) with the cedant —
  the recoverable and the cash don't move together.
- **Group vs entity** — intercompany pooling cessions net to zero at the group but are
  large at each entity; use the consolidated view for a group question.

## 8. How this maps to the warehouse

- `insurer_xbrl_facts` `reinsurance` (`premiums_ceded`, `ceded_premiums_earned`,
  `recoverable_unpaid`) + `premiums` (net) give the cession rollup; gross is derived.
- Recoverable leverage uses `statutory_facts.surplus`; the provision-for-reinsurance
  charge is a line in [[statutory-gaap-bridge]].
- Retroactive covers are a reserve-adequacy signal — cross-read with
  [[reserving-chain-ladder]] and the `disclosure_sentiment` reserve tone.
- The net-vs-gross combined ratio feeds [[combined-ratio-bridge]] and the
  `underwriting_results` topic.
