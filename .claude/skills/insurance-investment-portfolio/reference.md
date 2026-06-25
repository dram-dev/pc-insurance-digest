# Insurance investment portfolio & ALM — reference

Deeper backing for [SKILL.md](SKILL.md): the float identity, the reinvestment dynamic,
duration/convexity mechanics, the economic-vs-GAAP surplus distinction, credit quality,
and the worked example the script's `--demo` reproduces.

## 1. Float and cost of float

**Float** is the policyholder money an insurer holds between collecting premium and
paying claims — it funds the investment portfolio at someone else's expense.

```
float ≈ loss & LAE reserves + unearned premium reserve
        − reinsurance recoverables − DAC − agents' balances − prepaid reinsurance
cost of float = − underwriting profit / average float
```

- **Negative cost of float** (underwriting profit) = the carrier is *paid* to hold the
  money — it earns the entire investment return on borrowed funds at a negative interest
  rate. This is the Buffett thesis: float is leverage with a negative cost.
- **Positive cost of float** (underwriting loss) = the float costs that much per year;
  the investment yield must exceed it for the insurer to create value.

Float grows with the book and with reserve lengthening (long-tail lines hold float
longer). Connect to [[combined-ratio-bridge]] (the underwriting-profit numerator) and
[[reserving-chain-ladder]] (the reserve duration that sets how long float is held).

## 2. Net investment income, book yield, new-money yield

```
book yield     = net investment income / average invested assets
new-money yield = current reinvestment rate on maturing/ new cash
reinvestment drift = new-money − book
investment leverage = invested assets / equity
NII contribution to pretax ROE = NII / equity = book yield × investment leverage
```

The portfolio reprices **slowly** — only maturing bonds and new premium cash get the
new-money rate, so book yield trails new-money. After 2022's rate jump, book yields are
a multi-year tailwind climbing toward new-money; when rates fall, book yield is sticky
high then fades. The **drift** is the sign and rough size of the NII trajectory *before*
any new capital. The leverage identity is why a 4% yield can add ~10 pts to ROE — the
asset base is multiples of equity.

## 3. Duration, convexity, and the ALM gap

```
duration gap = asset duration − liability duration
ΔP/P ≈ − modified duration × ΔY      (+ ½·convexity·ΔY² for large moves)
```

P&C liabilities are **short-to-medium** — claims pay over a few years (short-tail
property faster, long-tail casualty slower). Insurers **match** asset duration to
liability (reserve) duration so a rate move revalues both sides together, protecting
**economic surplus**. The duration gap is the residual rate bet:
- gap > 0 (assets longer) → rising rates hurt economic surplus.
- gap < 0 (assets shorter) → falling rates hurt; reinvestment risk.

Convexity matters for large shocks and for callable/MBS holdings (negative convexity);
for a parallel ±100bp screen, modified duration is enough.

## 4. AOCI rate shock — GAAP vs economic

Available-for-sale (AFS) bonds are marked to fair value with the unrealized gain/loss
in **AOCI**, a component of GAAP equity:

```
AOCI hit ≈ − asset duration × ΔY × bond market value
```

But **P&C loss reserves are carried at nominal (undiscounted) value** in GAAP — they do
*not* mark up when rates rise. So a rate spike hits the asset side of GAAP equity in
full while the offsetting liability benefit is invisible:

```
economic surplus change ≈ − (Dₐ·A − D_L·L) × ΔY
```

The gap between the AOCI hit and the economic change is the **unrecognized liability
benefit** — GAAP book value *overstates* the economic damage of rising rates. This was
the defining 2022–2023 story: double-digit % AOCI book-value declines, far smaller
economic impairment, and **statutory surplus untouched** (SAP holds bonds at amortized
cost — see [[statutory-gaap-bridge]]). Held-to-maturity (HTM) bonds aren't marked at all
in GAAP, so the AFS/HTM split changes how much of the move shows up.

## 5. Composition & credit quality

The portfolio mix drives both yield and the RBC/BCAR asset charge:
- **Investment-grade fixed income** dominates (the regulatory and rating-agency capital
  charge rises steeply below investment grade — R1/R2 in [[insurer-capital-adequacy]]).
- **Allocation** (govt / corporate / muni / structured / equities / alternatives), the
  `fv_level` (fair-value hierarchy — Level 3 = illiquid/marked-to-model), and credit
  migration are the risk reads. The `investment_portfolio` facts carry instrument and
  `fv_level` dimensions for this cut.
- Realized gains/losses (`investment_gains`) and the AOCI balance (`aoci`) are the
  *recognized* and *unrecognized* halves of total return — separate them.

## 6. Worked example (the script's `--demo`)

Inputs ($M): reserves 30,000 · UPR 8,000 · recoverables 5,000 · DAC 2,500 · UW profit
800 · invested assets 60,000 · NII 2,400 · new-money 5.5% · equity 25,000 · asset dur
4.5y · liability dur 3.0y · bond MV 50,000 · shock +100bp · liability MV 30,500.

- **float** = 30,000 + 8,000 − 5,000 − 2,500 = **30,500**; cost of float = −800/30,500 =
  **−2.62%** (paid to hold).
- **book yield** = 2,400/60,000 = **4.0%** vs new-money 5.5% → **+150bp** reinvestment
  drift. Investment leverage 60,000/25,000 = **2.40×** → NII adds 4.0%·2.40 = **9.6%** to
  pretax ROE.
- **duration gap** = 4.5 − 3.0 = **+1.5y**. AOCI hit = −4.5·0.01·50,000 = **−2,250**
  (−9.0% of equity). Economic change = −(4.5·50,000 − 3.0·30,500)·0.01 = **−1,335** —
  GAAP overstates the hit by ~$915M because reserves aren't marked.

## 7. Pitfalls checklist

- **AFS vs HTM vs trading** — only AFS flows to AOCI; HTM isn't marked in GAAP (but the
  economic exposure is identical). Confirm the classification before reading the AOCI hit.
- **Duration is an estimate** — effective duration for callable/MBS shifts with rates
  (negative convexity); a single number understates the risk in a big move.
- **NII includes more than coupon** — dividends, limited-partnership/alternative income,
  and (sometimes) realized gains net in; check the disclosure before annualizing.
- **Float is approximate** — the exact deductions (prepaid reinsurance, agents' balances)
  vary by carrier; state the components you used.

## 8. How this maps to the warehouse

- `investment_income`, `investment_portfolio`, `investment_gains`, `aoci` + the float
  inputs (`unpaid_claims`, `premiums.unearned_premiums`, `reinsurance`, `dac`, `equity`)
  are all ingested — the float/yield/leverage blocks run from `--db`.
- AOCI ties to [[statutory-gaap-bridge]] (the GAAP-vs-statutory bond-carrying difference)
  and to [[insurer-valuation]] (AOCI swings book value, the P/B denominator).
- The investment-leverage term is the asset side of the [[insurer-valuation]] DuPont and
  a key input to [[cost-of-capital]] RAROC (investment income on allocated capital).
