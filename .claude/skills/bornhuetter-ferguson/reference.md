# Bornhuetter-Ferguson reference — theory, worked example, Cape Cod, BF vs CL

Deeper backing for [SKILL.md](SKILL.md). Read this for the mechanics, the Cape Cod
derivation, and the judgment of when BF beats chain-ladder (and when it doesn't).

## 1. The model in one paragraph

Chain-ladder estimates ultimate as `Latest × CDF` — it reads the *entire* ultimate
off the actuals, so a green accident year (small latest, large CDF) is estimated
almost entirely by extrapolation. **Bornhuetter-Ferguson** splits the ultimate into a
**reported** part and an **unreported** part. The reported part is `1/CDF` of ultimate
and we already see it (the latest actual). The unreported part is `(1 − 1/CDF)` of
ultimate, and instead of extrapolating it from the thin diagonal, BF takes it from an
**a-priori expected ultimate** (premium × expected loss ratio):

```
Ultimate_BF = Latest_actual            +   Apriori × (1 − 1/CDF)
              └ reported, from actuals ┘   └ unreported, from the a-priori ┘
IBNR_BF     = Apriori × (1 − 1/CDF)
```

So BF is a **credibility blend**: it gives full weight to actuals for the developed
portion and full weight to the a-priori for the undeveloped portion. The weight is
the development pattern itself — `Z = 1/CDF` is the implied credibility of the actuals.

## 2. Worked example (the script's `--demo`, reconciles to the chain-ladder skill)

Same triangle as [[reserving-chain-ladder]] reference §2:

| AY \ dev | 0 | 1 | 2 | 3 | premium |
|---|---|---|---|---|---|
| 2019 | 1,000 | 1,500 | 1,750 | 1,800 | 2,400 |
| 2020 | 1,200 | 1,800 | 2,100 | | 2,640 |
| 2021 | 1,100 | 1,650 | | | 2,200 |
| 2022 | 1,300 | | | | 2,600 |

Volume-weighted CDFs (from the chain-ladder skill): `CDF(0)=1.8, CDF(1)=1.2,
CDF(2)=1.02857, CDF(3)=1.0`. So **% unreported** `= 1 − 1/CDF`:

| AY | latest | CDF | %unrep | %developed |
|---|---|---|---|---|
| 2019 | 1,800 | 1.0000 | 0.0% | 100.0% |
| 2020 | 2,100 | 1.0286 | 2.8% | 97.2% |
| 2021 | 1,650 | 1.2000 | 16.7% | 83.3% |
| 2022 | 1,300 | 1.8000 | 44.4% | 55.6% |

**BF with a supplied ELR = 0.75** (a-priori ult = premium × 0.75):

| AY | apriori | %unrep | BF IBNR = apriori×%unrep | BF ult = latest + IBNR | CL ult (latest×CDF) |
|---|---|---|---|---|---|
| 2019 | 1,800 | 0.0% | 0 | 1,800 | 1,800 |
| 2020 | 1,980 | 2.8% | 55 | 2,155 | 2,160 |
| 2021 | 1,650 | 16.7% | 275 | 1,925 | 1,980 |
| 2022 | 1,950 | 44.4% | 867 | 2,167 | 2,340 |

Totals: latest 6,850 · **CL ult 8,280 (IBNR 1,430)** · **BF ult 8,047 (IBNR 1,197)** —
BF IBNR is **16% lower** than CL. The ~233 IBNR gap is concentrated in the green years:
AY2022 ≈173 (CL 2,340 vs BF 2,167), AY2021 55, AY2020 5 — summing to the 233 total.
Mature AY2019 is identical; the green years are where BF acts. This is the point: BF
refuses to let a 1,300 diagonal × 1.8 CDF dictate the whole 2022 ultimate.

## 3. Cape Cod (Stanard-Bühlmann) — let the data pick the ELR

The obvious objection to BF is "where did ELR = 0.75 come from?" **Cape Cod** answers
it by deriving the ELR from the triangle:

```
ELR_CapeCod = Σ_AY (latest actual)  /  Σ_AY (premium × %developed_AY)
            = Σ actual losses  /  Σ "used-up" (on-level, developed) premium
```

It is a premium-weighted average loss ratio, where each AY's premium is discounted by
how developed (hence credible) it is — a green AY barely counts. For the demo:

- Σ actual = 1,800 + 2,100 + 1,650 + 1,300 = **6,850**
- used-up premium = premium × %developed:
  2,400×1.0 + 2,640×0.9722 + 2,200×0.8333 + 2,600×0.5556 = 2,400 + 2,566.7 + 1,833.3 +
  1,444.4 = **8,244.4**
- **ELR = 6,850 / 8,244.4 = 0.8309**

Feed that back through BF (a-priori = premium × 0.8309) and the total BF ultimate is
**8,175.7 (IBNR 1,325.7)** — between the flat-0.75 BF (8,047) and pure CL (8,280),
because Cape Cod's ELR is closer to what the triangle actually implies. Cape Cod is
the right default when you have no externally credible plan loss ratio; a supplied ELR
is better when you *do* know rate adequacy changed and the historical ratio is stale.

## 4. BF vs chain-ladder vs Cape Cod — when each wins

| Situation | Prefer | Why |
|---|---|---|
| Mature AY, low %unrep, stable factors | Chain-ladder | Actuals are credible; a-priori adds nothing |
| Green AY, high %unrep × large CDF | **BF** | CL over-reacts to a thin diagonal |
| Factor on n=1–2 link ratios (the youngest steps) | **BF** | The CDF itself is unreliable; don't lever it |
| No credible external ELR | **Cape Cod** BF | Derive the a-priori from the triangle |
| Known rate-adequacy / mix shift | BF w/ supplied ELR | The historical (Cape Cod) ratio is stale |
| Long-tail beyond triangle width | BF **+ tail** | %unrep must include the tail or BF under-reserves |
| Need a range, not a point | Mack / bootstrap | BF is still a point estimate (see §6) |

Rule of thumb actuaries use: **CL on the bottom-left (old, developed) AYs, BF on the
top-right (young, green) AYs**, often with the same selected pattern — this is the
"Benktander" intuition (one BF iteration toward CL). Don't apply one method blindly to
every row.

## 5. Sign / metric pitfalls

- **Paid vs incurred** — BF IBNR on an **incurred** triangle is *pure IBNR* (case
  reserves already inside incurred); on a **paid** triangle it is *total unpaid*
  (case + pure IBNR). The ELR basis must match the metric.
- **ELR must be on the same level as the premium** — if premium is net earned, the ELR
  is a net loss (or loss+LAE) ratio. Don't pair a gross ELR with net premium.
- **A-priori ultimate, not a-priori IBNR** — BF multiplies the *expected ultimate* by
  %unreported. Supplying expected IBNR directly double-discounts.
- **Negative development** — if a factor < 1 (salvage/subro, incurred releases), CDF
  can dip below 1 and %unrep goes negative; BF then *reduces* the a-priori
  contribution, which is correct but worth a sanity check.

## 6. Uncertainty

BF, like CL, is a point estimate. The standard error around a BF ultimate is generally
*smaller* than CL's for green years (it leans on the more stable a-priori) but adds
**parameter risk in the ELR**. For a full stochastic treatment — Mack-style standard
errors, bootstrap distributions, the Clark / Bornhuetter-Ferguson stochastic models —
use `chainladder-python` (`casact/chainladder-python`, the documented upgrade path in
`src/digest/reserving.py`). The CV the script reports per factor is the same quick
volatility proxy as the chain-ladder skill; high CV on a step is another reason to
prefer BF there.

## 7. How this maps to the warehouse

- The pipeline's `digest reserving` (`src/digest/reserving.py`) stores **pure
  chain-ladder** ultimate/IBNR in `reserving_signals` — there is no BF in the
  pipeline. This skill is how the Analyst *reinterprets* a stored signal: re-develop
  the same triangle, apply BF/Cape Cod, and decide whether a stored adverse/favorable
  read on the youngest AYs is a real reserve move or CL over-reaction on a thin
  diagonal.
- The development factors and CDFs here are computed by the **identical** volume-
  weighted method as `reserving.py::chain_ladder`, so for a clean triangle the CL
  column in this script equals the stored `reserving_signals` total to rounding.
- Premium is the one input the warehouse doesn't hold — source it from the carrier's
  EDGAR content / investor supplement (net earned premium by AY for the line), the
  same place the combined-ratio bridge gets its figures (see [[combined-ratio-bridge]]).
