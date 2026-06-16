# P&C insurer liquidity & treasury — reference

Deeper backing for [SKILL.md](SKILL.md): the HoldCo/OpCo structure, the dividend-
capacity rules, coverage and runway, contingent capital, debt laddering, and the worked
example the script's `--demo` reproduces.

## 1. The HoldCo / OpCo structure — why cash is trapped

A public insurer is a **holding company** that owns one or more **regulated insurance
operating companies**. The cash and invested assets live in the OpCos (backing
policyholder reserves); the **holdco** holds the stock of the subs plus a modest cash
buffer, and owes the **debt interest** and the **shareholder dividend**. The only
organic way cash reaches the holdco is a **dividend from a regulated sub** — and a
state insurance regulator caps how much a sub may pay without approval. Hence: a group
can be capital-rich but holdco-cash-poor. **Solvency** (is there enough capital, RBC)
and **liquidity** (can the cash reach the obligation) are different questions.

## 2. Statutory dividend capacity

The NAIC model holding-company act defines an **extraordinary dividend** as one
exceeding the **greater of**:
- **10% of the sub's prior-year-end statutory surplus**, or
- **the sub's prior-year statutory net income**.

Anything at or below that threshold is an **ordinary** dividend (no prior approval);
above it needs the domiciliary regulator's sign-off. **State variation is real** — some
states use the *lesser* of the two, some substitute **net investment income** for net
income, and several add look-back tests. Always note the domicile. The capacity is also
constrained by **unassigned funds** (positive earned surplus) — a sub with negative
unassigned surplus can't pay an ordinary dividend at all.

This capacity is the **ceiling on the holdco's organic cash inflow**, and so the hinge
of the whole liquidity picture.

## 3. HoldCo coverage & cash runway

```
holdco sources/yr = upstream dividends (≤ capacity) + holdco investment income
holdco uses/yr    = interest + common (& preferred) dividends + holdco opex
net holdco cashflow = sources − uses
interest coverage = sources / interest
total-obligation coverage = sources / uses
cash runway = holdco liquid assets / annual drain   (when sources < uses)
```

- **Interest coverage** (or fixed-charge coverage including preferred) is the rating-
  agency debt-service screen — usually comfortable because interest is small relative
  to upstream dividends.
- **Total-obligation coverage** folds in the **common dividend** — discretionary in law
  but sticky in practice (cutting it is a major signal). When it's < 1×, the holdco is
  funding part of the payout from its cash buffer; the **runway** says for how long
  before it must raise sub dividends (depleting statutory surplus, pressuring RBC —
  [[insurer-capital-adequacy]]) or cut the dividend.
- **Double leverage** ([[cost-of-capital]]) makes this tighter: holdco debt raised to
  down-stream sub equity is serviced from those same capped sub dividends.

## 4. Sources & uses, and contingent capital

Beyond the dividend loop, the full liquidity picture is a sources-and-uses statement:
- **Sources:** premium cash, investment income + bond maturities (at the OpCo), upstream
  dividends, debt issuance, asset sales.
- **Uses:** claim & LAE payments, operating expenses, ceded premium, interest, dividends,
  **debt maturities** (the refinancing ladder), buybacks.

**Contingent capital** backstops a spike: **FHLB** membership (borrow against the bond
portfolio), a bank **revolver**, **cat bonds / ILS** (pay out on a trigger), and
pre-arranged **contingent equity**. These convert illiquid capital into fast cash
without a fire sale.

## 5. Catastrophe liquidity

A catastrophe is a **liquidity** event before it is a solvency event: claims must be
**paid in cash quickly**, well before reinsurance recoveries arrive (the cedant pays
first, then collects — a recoverable timing gap; see [[reinsurance-accounting]]). The
carrier may have to **liquidate investments** — realizing AOCI losses if rates have
risen ([[insurance-investment-portfolio]]) — or draw FHLB/revolver/cat-bond proceeds.
Stress it: a **1-in-100 net cat loss** vs **liquid investments + contingent capital**.
Coverage < 1× is a genuine gap even when surplus is ample.

## 6. Worked example (the script's `--demo`)

Inputs ($M): prior surplus 6,000 · prior statutory NI 700 · holdco liquid 1,500 ·
interest 200 · common dividends 900 · holdco opex 50 · holdco investment income 80 ·
1-in-100 net cat 2,500 · liquid investments 8,000 · contingent capital 1,500.

- **dividend capacity** = max(10%·6,000 = 600, 700) = **700**.
- **holdco**: sources = 700 + 80 = **780**; uses = 200 + 900 + 50 = **1,150**; net CF =
  **−370**. Interest coverage = 780/200 = **3.9×** (solid); total coverage = 780/1,150 =
  **0.68×** — the common dividend is partly cash-funded. Runway = 1,500/370 = **4.1y**.
- **cat liquidity**: available = 8,000 + 1,500 = 9,500 vs 2,500 net cat → **3.8×** (ample).

Story: debt is safe, capital is fine, but the **shareholder dividend** outruns organic
upstream cash by ~$370M/yr — a 4-year runway before the sub dividend must rise (pressuring
RBC) or the payout is cut. The watch item is the dividend, not solvency.

## 7. Pitfalls checklist

- **Statutory NI ≠ GAAP NI** — the dividend-capacity rule is on *statutory* net income;
  the warehouse's `segment_results.net_income` is GAAP, a proxy only (the script flags it).
- **Per-entity, not group** — dividend capacity is computed at each OpCo against *its*
  surplus; a group with several subs sums the per-sub capacities, and a sub with negative
  unassigned surplus contributes zero.
- **Domicile matters** — greater-of vs lesser-of, NI vs net investment income, look-back
  windows. State the rule used.
- **Reinsurance recoverable timing** — cat cash goes out before recoveries come in; don't
  net the recoverable against the immediate liquidity need.
- **Realized-loss feedback** — selling bonds for cat cash in a high-rate environment
  crystallizes AOCI losses, hitting GAAP equity just when capital matters.

## 8. How this maps to the warehouse

- `statutory_facts.surplus` (capacity base), `insurer_xbrl_facts` `liquidity`
  (cash, dividends) + `capital_structure.interest_expense` (debt service) feed the
  computable parts; statutory NI, opex split, contingent capital and the cat PML are
  supplied.
- Ties to [[insurer-capital-adequacy]] (raising sub dividends depletes surplus/RBC),
  [[cost-of-capital]] (double leverage), [[insurance-investment-portfolio]] (asset
  liquidation / AOCI), and [[reinsurance-accounting]] (recoverable timing on a cat).
