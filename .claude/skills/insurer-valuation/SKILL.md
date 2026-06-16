---
name: insurer-valuation
description: >-
  Valuation for a P&C insurer — the P/B↔ROE justified multiple, a residual-income
  (excess-return) intrinsic value, a reconciling dividend-discount model, an
  operating-ROE DuPont split, and a peer P/B-on-ROE regression. Use when a question
  involves whether a carrier is cheap or expensive, price-to-book vs ROE, fair value,
  intrinsic value, justified multiple, cost-of-equity-relative return, through-the-
  cycle normalized earnings, or relative value vs peers / IAK. Insurers are valued off
  book value and ROE vs cost of equity — not EBITDA multiples.
---

# P&C insurer valuation

Book value is economically meaningful for a financial, so a P&C insurer is valued off
**ROE versus its cost of equity**, capitalized onto book — not off revenue/EBITDA
multiples. The central fact: a carrier earning its cost of equity is worth exactly
book (P/B = 1); the premium or discount to book is the **excess return** `ROE − Ke`
capitalized. This skill computes the justified P/B, an explicit residual-income
intrinsic value, a reconciling DDM, the operating-ROE quality split, and a peer
regression, then triangulates against the market price.

Full theory — the clean-surplus identity that makes the three models agree, the fade,
normalized earnings, and sum-of-the-parts — is in [reference.md](reference.md).

## Where the data lives

- **`insurer_xbrl_facts`** — `equity.common_equity` (book value), `equity.shares_outstanding`,
  `segment_results.net_income` (→ ROE). The `equity` dataset was added so this skill
  has a real book-value/per-share anchor.
- **`prices`** — latest `close` for the market price → market P/B, P/E, upside.
- **Cost of equity (r) and sustainable growth (g) you supply** — get `r` from
  [[cost-of-capital]] (CAPM), and `g` from sustainable growth `b·ROE` or a forecast.
  `statutory_facts.surplus` is a statutory-capital cross-check.

## Procedure

```bash
python3 .claude/skills/insurer-valuation/scripts/insurer_valuation.py \
    --book-value 25000 --shares 250 --net-income 3500 \
    --cost-of-equity 0.09 --growth 0.04 --price 180 \
    --peers "A:1.5:0.11,B:2.2:0.15,C:1.8:0.13,D:2.6:0.17"
# from the warehouse, supply only r and g:
python3 …/insurer_valuation.py --db data/state.db --insurer PGR -r 0.09 -g 0.05
```

Read, in order:
- **justified P/B = (ROE − g)/(Ke − g)** — the multiple the returns justify.
- **residual-income value** = book + PV of excess returns `(ROEₜ − Ke)·BVₜ₋₁` + terminal.
- **DDM** — reconciles (clean surplus): for constant ROE all three give the *same*
  number; divergence means you fed an inconsistent g/payout.
- **operating vs total ROE** (if components supplied) — strips realized gains / AOCI.
- **market P/B, upside, P/E**, and the **peer P/B-on-ROE line** placing the carrier.

`--demo` is the verified worked example; `--stdin` takes JSON.

## Interpreting the result (judgment, not arithmetic)

- **P/B and ROE must be read together.** A 2.5× P/B is not "expensive" if the carrier
  sustainably earns 18% on equity; a 1.0× P/B is not "cheap" if it earns 7% against a
  9% cost of equity. The peer regression makes this explicit — the residual from the
  P/B-on-ROE line is the real relative-value signal (PGR's premium multiple is *earned*
  by its ROE; a low-ROE carrier at the same multiple would be rich).
- **Normalize the ROE through the cycle.** A hard-market peak ROE inflated by reserve
  releases and a light cat year is not sustainable — value off a **normalized**
  combined ratio (mid-cycle, normal cat load; see [[combined-ratio-bridge]] /
  [[reserving-chain-ladder]]) before capitalizing. Valuing a cyclical peak as perpetual
  is the classic insurance-valuation error.
- **Operating ROE > total ROE quality.** If reported ROE leans on realized investment
  gains or favorable development, the operating split exposes it — capitalize the
  *operating* return, treat the rest as non-recurring.
- **ROE < Ke ⇒ P/B < 1, structurally.** The script warns when the spread is negative:
  the firm destroys value and *should* trade below book; a market P/B > 1 on a
  sub-cost-of-equity ROE is the market pricing in a turnaround you must justify.
- **g < Ke always**, and g can't durably exceed `b·ROE`. A growth assumption above the
  sustainable rate silently inflates every model.

## Output discipline

Lead with the justified-vs-market P/B and the ROE-vs-Ke spread that drives it. E.g.
*"On a 14% normalized ROE and 9% cost of equity, PGR's justified P/B is 2.0× (excess
return 5 pts capitalized); residual-income and DDM agree at ~$50B / $200 a share. The
market pays 1.8× → ~11% upside, and the peer P/B-ROE line says the same (fair 2.0× at
this ROE). The risk is the 14% is a hard-market figure — normalize it and the upside
narrows."*
