# GAAP equity ↔ statutory surplus — reference

Deeper backing for [SKILL.md](SKILL.md): the two frameworks, every reconciling item
with its sign and SSAP basis, the change-in-surplus account, and the worked example
the script's `--demo` reproduces.

## 1. Two accounting frameworks, two capital numbers

| | GAAP (10-K) | Statutory / SAP (NAIC annual statement) |
|---|---|---|
| Audience | investors | regulators (solvency) |
| Bias | going-concern, matching | conservative, liquidation |
| Capital line | **shareholders' equity** | **policyholders' surplus** |
| Bonds (high quality) | AFS at **fair value**, unrealized in AOCI | **amortized cost** (NAIC 1–2) |
| Acquisition costs | **DAC** asset, amortized | **expensed immediately** |
| Non-admitted assets | on balance sheet | **excluded from surplus** |
| Deferred tax asset | recognized (valuation allowance) | **admitted only** to an SSAP-101 limit |

Statutory surplus is what **RBC**, **premium-to-surplus** and **dividend capacity**
use. GAAP equity is what **P/B**, **ROE** and most valuation use. They answer
different questions; never substitute one for the other.

## 2. The reconciling items (GAAP common equity → statutory surplus)

`surplus = GAAP equity − AOCI − DAC − goodwill/intangibles − non-admitted DTA −
provision for reinsurance − other non-admitted`

| Item | Why | Sign (to surplus) |
|---|---|---|
| **AOCI** (AFS bond unrealized) | SAP carries bonds at amortized cost; reverse GAAP's fair-value AOCI | **−AOCI** (a loss, AOCI<0, *adds* back) |
| **DAC** | GAAP asset; SAP expenses acquisition cost | **− DAC balance** |
| **Goodwill & intangibles** | largely non-admitted under SAP | **−** |
| **Non-admitted DTA** | SSAP 101 admits DTA only to a 3-part limit | **−** the disallowed portion |
| **Provision for reinsurance** | Schedule F charge for unauthorized / overdue recoverables | **−** |
| **Other non-admitted** | furniture/EDP, prepaids, agents' balances > 90 days, software | **−** |

Two items SAP carries the *same* as GAAP and so do **not** reconcile: common stock
held as an investment (both fair value) and loss reserves (both undiscounted nominal,
except a few long-tail discounting rules). The big mover is almost always **DAC**
(structural) and **AOCI** (rate-driven).

Note the AOCI sign carefully. AOCI is GAAP's *unrealized* AFS gain/loss. SAP doesn't
recognize it, so you remove it: `− AOCI`. When rates rose in 2022 the AFS portfolio
went to an unrealized **loss** (AOCI < 0), so `−AOCI` is **positive** — statutory
surplus sat *above* the AOCI-depressed GAAP equity. That divergence is the single
most important capital story of the 2022–2023 cycle.

## 3. The change-in-surplus account

The statutory analogue of a retained-earnings rollforward. Surplus moves by:

```
surplus_end = surplus_begin
  + statutory net income
  + change in net unrealized capital gains
  − stockholder dividends
  + capital & paid-in surplus contributed
  + change in net deferred income tax
  + change in non-admitted assets        (more non-admitted ⇒ negative)
  + change in provision for reinsurance
  + other / aggregate write-ins
```

Reading the drivers:
- **Net income** is *earned* capital — the highest-quality source. Note it already
  contains prior-year reserve development; a surplus rise driven by net income that is
  itself driven by reserve *releases* is lower quality than it looks (cross-check
  [[reserving-chain-ladder]] / [[combined-ratio-bridge]]).
- **Unrealized capital gains** are mark-to-market on the equity/illiquid book — can
  reverse next year; not durable capital.
- **Dividends** are the upstreaming to the parent (ties to [[insurer-liquidity]]
  dividend-capacity work).
- **Paid-in capital** is an external raise — often a tell that organic capital
  generation fell short of growth or absorbed a reserve charge / cat year.

## 4. Worked example (the script's `--demo`)

Bridge inputs ($M): GAAP equity 25,000 · AOCI −1,200 · DAC 3,000 · goodwill &
intangibles 1,500 · non-admitted DTA 400 · provision for reinsurance 100.

| Step | Effect | Running |
|---|---:|---:|
| GAAP shareholders' equity | | 25,000.0 |
| − AOCI (reverse to amortized cost) | +1,200.0 | 26,200.0 |
| − DAC (non-admitted) | −3,000.0 | 23,200.0 |
| − goodwill & intangibles | −1,500.0 | 21,700.0 |
| − non-admitted DTA | −400.0 | 21,300.0 |
| − provision for reinsurance | −100.0 | 21,200.0 |
| **= implied statutory surplus** | | **21,200.0** |

Reported statutory surplus 21,200 → residual 0. The $3.8B GAAP-over-statutory gap is
~80% DAC, with a partly offsetting +$1.2B AOCI bond-loss add-back.

Change demo ($M): begin 20,000 + net income 2,400 + unrealized 300 − dividends 1,200
+ paid-in 0 + deferred tax 150 − non-admitted 250 − provision 50 − other 150 = **end
21,200** (total change +1,200). The two demos are tied so the bridge endpoint equals
the change endpoint — net income (+200% of the change) is the engine, partly returned
to the parent as dividends (−100%).

## 5. Pitfalls checklist

- **Group GAAP vs legal-entity SAP.** The 10-K is consolidated group; statutory
  surplus is **per legal entity** and the statement is filed by each insurance sub.
  A group-equity-to-one-sub-surplus bridge is apples-to-oranges — aggregate the subs
  or compare group GAAP to *combined* statutory surplus.
- **AVR/IMR are life-company items**, not P&C — don't add them to a P&C bridge.
- **Surplus notes** count toward statutory surplus but are debt for GAAP/leverage —
  a reconciling item in both directions; flag if the carrier has them.
- **A residual is information, not failure.** > 2% of surplus unexplained means an
  item is missing or mis-estimated — name the likely culprit (usually DTA admittance
  or Schedule F) rather than fudging.
- **Reserve adequacy is *inside* net income**, both bases — it is not a separate
  reconciling line. Adverse development depresses both GAAP equity and statutory
  surplus through earnings.

## 6. How this maps to the warehouse

- GAAP side is ingested: `insurer_xbrl_facts` `equity` / `aoci` / `dac`. The XBRL
  `equity` dataset was added specifically so this bridge (and [[insurer-valuation]])
  have a real book-value anchor — see the concept registry in
  `src/digest/parse/xbrl_facts.py`.
- `statutory_facts.surplus` is the reported-surplus cross-check (sparse — mostly
  mutuals + a few stock carriers via NAIC InsData / III).
- Feeds every downstream capital skill: statutory surplus is the denominator in
  [[insurer-capital-adequacy]] (RBC, premium-to-surplus), the dividend-capacity base
  in [[insurer-liquidity]], and the STAT-capital cross-check in [[insurer-valuation]].
- The DAC and AOCI lines connect to [[insurance-investment-portfolio]] (AOCI is the
  AFS-bond rate sensitivity) and [[combined-ratio-bridge]] (DAC amortization is an
  acquisition-expense item).
