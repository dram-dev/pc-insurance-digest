---
name: insurer-capital-adequacy
description: >-
  Solvency and capital adequacy for a P&C insurer — NAIC Risk-Based Capital (the
  R0–R5 charges, the covariance adjustment, ACL and the regulatory action levels),
  AM Best's BCAR, and statutory leverage / IRIS ratios (premium-to-surplus,
  reserves-to-surplus, net leverage). Use when a question involves whether a carrier
  is adequately capitalized, RBC ratio, BCAR, premium-to-surplus or reserve leverage,
  surplus adequacy, regulatory action levels, or how much capital a book of business
  consumes. Statutory surplus is the denominator — get it from [[statutory-gaap-bridge]]
  before reading these ratios.
---

# P&C capital adequacy — RBC, BCAR, leverage

How much capital does this insurer need, and does it have it? Three lenses: the
regulator's **NAIC RBC**, the rating agency's **AM Best BCAR**, and the quick
**leverage / IRIS** ratios. The helper computes whichever blocks the inputs support
in one pass; you supply the structural read.

Full theory — the RBC formula, why R4 dominates for P&C, the action-level ladder, the
IRIS battery, BCAR's VaR construction — is in [reference.md](reference.md).

## Where the data lives

- **Leverage ratios are computable from the warehouse:** `statutory_facts.surplus`
  (denominator), `insurer_xbrl_facts` `premiums.premiums_written_net` (NWP),
  `unpaid_claims.liability_net` (net reserves). `--db --insurer X` pulls these.
- **RBC components (R0–R5) and BCAR (available / required capital) are NOT ingested** —
  they live in the statutory RBC pages / a Best's report. Supply them as flags; the
  skill computes the rest. Be explicit that an RBC number is the filer's, not yours.

## Procedure

```bash
python3 .Codex/skills/insurer-capital-adequacy/scripts/capital_adequacy.py \
    --r0 50 --r1 600 --r2 500 --r3 700 --r4 2200 --r5 1500 --tac 6000 \
    --nwp 4000 --gwp 5000 --reserves 10000 --surplus 6000 \
    --available-capital 6500 --net-required-capital 4500 --bcar-var 99.6
# warehouse leverage only (supply RBC/BCAR components to add those blocks):
python3 …/capital_adequacy.py --db data/state.db --insurer TRV
```

Read the three blocks:
- **RBC**: `RBC after covariance = R0 + √(R1²+R2²+R3²+R4²+R5²)`; `ACL = ½·RBC`;
  **RBC ratio = TAC / ACL**. Action ladder: ≥200% no action · 150–200% Company Action ·
  100–150% Regulatory Action · 70–100% Authorized Control · <70% Mandatory Control.
- **Leverage / IRIS**: premium-to-surplus, reserves-to-surplus, net leverage, with
  IRIS usual-range flags.
- **BCAR** = (available − required capital) / available at a VaR level → Best's bands
  (>25% Strongest, 10–25% Very Strong, 0–10% Strong/Adequate, <0 Weak).

`--demo` runs the verified worked example; `--stdin` takes a JSON payload.

## Interpreting the result (judgment, not arithmetic)

- **R4 (reserve risk) dominates P&C RBC** — for a long-tail writer it's the biggest
  charge, and it's the one most exposed to adverse development. A carrier whose RBC
  looks fine but whose reserves are deteriorating (cross-check [[reserving-chain-ladder]])
  is running down a buffer the formula already counted. The **covariance benefit**
  (Σ Rᵢ − after-covariance RBC) is real diversification, not slack.
- **The RBC ratio convention trips people up.** "200%" is 200% **of ACL**, which equals
  the after-covariance RBC itself (ACL = ½·RBC). Carriers often headline a number vs
  Company Action Level — confirm the base before comparing two carriers' "RBC ratios."
- **Leverage ratios are fast but coarse.** Premium-to-surplus ~1:1 is conservative,
  >3:1 aggressive; but a low-volatility personal-auto book can safely run more leverage
  than a cat-exposed property book. Read leverage *through the line of business and the
  cat exposure*, not against a single threshold.
- **TAC ≠ GAAP equity.** Total Adjusted Capital is built off **statutory surplus** (plus
  certain reserves). Don't feed GAAP book value as TAC — run [[statutory-gaap-bridge]]
  first. A carrier can look well-capitalized on GAAP equity yet thin on statutory TAC.
- **The three measures can disagree** — RBC is a fixed-factor formula, BCAR is a
  VaR/cat-stress model, IRIS is a screen. When they diverge, the cat-stressed BCAR
  usually carries the most information for a property writer; say which you trust and why.

## Output discipline

Lead with the headline ratio, the action/assessment band, and the binding constraint.
E.g. *"TRV RBC 412% of ACL (well clear of the 200% action level); R4 reserve risk is
~75% of the pre-covariance charge, so adequacy hinges on reserve stability. Net
premium-to-surplus 0.67× and BCAR 30.8% (Strongest) corroborate — capital is strong,
the watch item is adverse development, not leverage."*
