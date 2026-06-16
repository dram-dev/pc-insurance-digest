# P&C capital adequacy — reference

Deeper backing for [SKILL.md](SKILL.md): the RBC formula and action ladder, the IRIS
ratios, BCAR's construction, and the worked example the script's `--demo` reproduces.

## 1. NAIC Risk-Based Capital (P&C formula)

RBC sizes the capital a specific book of business requires, then compares it to the
capital actually held (Total Adjusted Capital). Six risk charges:

| Charge | Risk | Typical driver |
|---|---|---|
| **R0** | affiliate / off-balance-sheet | insurance subsidiaries, guarantees |
| **R1** | fixed-income asset risk | bond default by NAIC designation |
| **R2** | equity / other asset risk | common stock, real estate, Schedule BA |
| **R3** | credit risk | **reinsurance recoverables**, other receivables |
| **R4** | **reserve risk** | adverse development on net loss & LAE reserves |
| **R5** | **premium / underwriting risk** | net written premium by line |

The charges are combined with a **covariance (square-root) adjustment** that credits
diversification — the risks aren't perfectly correlated, so the total is less than the
sum:

```
RBC (after covariance) = R0 + √(R1² + R2² + R3² + R4² + R5²)
ACL  (Authorized Control Level) = ½ × RBC after covariance
RBC ratio = TAC / ACL
```

For P&C, **R4 (reserves) and R5 (premium) dominate** — the business risk, not the
asset risk, is what consumes capital (the reverse of a life insurer, where C-3 interest
risk leads). That's why reserve adequacy is the hinge of P&C solvency.

**Total Adjusted Capital (TAC)** ≈ statutory surplus + certain reserves (e.g. some
non-tabular discount). It is built off **statutory** capital — see
[[statutory-gaap-bridge]]; never substitute GAAP equity.

### Action ladder (TAC as a multiple of ACL)

| TAC / ACL | Level | Consequence |
|---|---|---|
| ≥ 200% | No action | adequately capitalized |
| 150–200% | Company Action Level | insurer files a corrective plan |
| 100–150% | Regulatory Action Level | regulator examines / orders corrective action |
| 70–100% | Authorized Control Level | regulator **may** take control |
| < 70% | Mandatory Control Level | regulator **must** take control |

"200% of ACL" = the after-covariance RBC itself (because ACL = ½·RBC). Companies
sometimes quote a ratio vs **Company Action Level** (= 2×ACL) instead — always confirm
the base. Healthy P&C carriers run ~300–450% of ACL.

## 2. Statutory leverage & IRIS

Fast surplus-adequacy screens (the NAIC IRIS battery has 13; the capital-relevant ones):

```
gross premium to surplus = gross written premium / surplus   (IRIS 1, usual < 900%)
net premium to surplus   = net written premium  / surplus    (IRIS 2, usual < 300%)
reserves to surplus      = net loss & LAE reserves / surplus
net leverage             = (NWP + reserves)      / surplus
```

Premium-to-surplus ~1:1 is conservative; 2:1 normal; >3:1 aggressive. **But leverage
tolerance is line-dependent:** a stable personal-auto book safely runs higher leverage
than a volatile cat-property or long-tail-liability book. IRIS flags an *unusual* value,
not necessarily a *bad* one — it's a screen that points the examiner, not a verdict.

## 3. AM Best BCAR

Best's Capital Adequacy Ratio is a **stochastic, VaR-based** capital model (richer than
RBC's fixed factors — it stress-tests the cat tail):

```
BCAR = (available capital − net required capital) / available capital
```

evaluated at several confidence levels (VaR 95 / 99 / 99.5 / **99.6**). The published
assessment reads the BCAR at the 99.6 level:

| BCAR @ 99.6 | Best's capital assessment |
|---|---|
| > 25% | Strongest |
| 10–25% | Very Strong |
| 0–10% | Strong / Adequate |
| < 0 | Weak (capital below required) |

Because BCAR explicitly models the **catastrophe PML in the required capital**, it is
usually the most informative of the three for a property-cat writer; RBC's fixed factors
under-weight tail cat risk. Connect the cat side to the catastrophe reasoning in the
analyst persona (PML / OEP-AEP) and to `cat_load` regime.

## 4. Worked example (the script's `--demo`)

Inputs ($M): R0 50 · R1 600 · R2 500 · R3 700 · **R4 2,200** · **R5 1,500** · TAC
6,000 · NWP 4,000 · GWP 5,000 · net reserves 10,000 · surplus 6,000 · available capital
6,500 · net required capital 4,500 · VaR 99.6.

- **RBC**: √(600²+500²+700²+2,200²+1,500²) = 2,861.8; + R0 50 = **2,911.8** after
  covariance (diversification benefit 5,550 − 2,911.8 = **2,638.2**). ACL = 1,455.9.
  RBC ratio = 6,000 / 1,455.9 = **412.1% of ACL** → No action. R4+R5 are ~90% of the
  pre-covariance charge — reserve + premium risk dominate.
- **Leverage**: gross P/S 0.83×, **net P/S 0.67×**, reserves/surplus 1.67×, net leverage
  2.33× — all conservative, no IRIS flag.
- **BCAR**: (6,500 − 4,500)/6,500 = **30.8%** → Strongest.

All three agree: strong capital; the watch item is reserve risk (R4), not leverage.

## 5. Pitfalls checklist

- **Group vs legal entity.** RBC and IRIS are filed per insurance entity; the group
  number is a roll-up. A holding-company question about *fungible* capital also needs
  the dividend-capacity / double-leverage view — see [[insurer-liquidity]] and
  [[cost-of-capital]].
- **TAC is statutory, not GAAP.** Running RBC off GAAP equity overstates adequacy
  (GAAP equity > statutory surplus by ~DAC).
- **A high RBC ratio with deteriorating reserves is a melting buffer** — R4 already
  assumes reserves are adequate; if they're not, the true ratio is lower. Cross-check
  [[reserving-chain-ladder]] / `reserving_signals`.
- **Don't compare RBC to BCAR point-for-point** — different scales and constructions.
  Compare each to its own band.

## 6. How this maps to the warehouse

- Leverage block is fully computable: `statutory_facts.surplus`, `insurer_xbrl_facts`
  `premiums.premiums_written_net`, `unpaid_claims.liability_net`.
- R3 (credit) ties to the reinsurance recoverables in [[reinsurance-accounting]]; R4
  to [[reserving-chain-ladder]]; the surplus denominator to [[statutory-gaap-bridge]].
- RBC/BCAR component inputs are exogenous (statutory RBC pages / Best's report) — the
  skill is explicit that those are supplied, not derived.
