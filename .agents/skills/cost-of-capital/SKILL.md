---
name: cost-of-capital
description: >-
  Cost of capital for a P&C insurer — CAPM cost of equity, after-tax cost of debt,
  WACC, capital structure and double leverage, RAROC / economic capital, and the
  economic-profit (ROE − cost of equity) spread. Use when a question involves a
  carrier's cost of equity or WACC, hurdle rate, equity beta, capital structure /
  financial leverage, holding-company double leverage, risk-adjusted return on
  capital, economic value added, or whether a business line / acquisition clears its
  cost of capital. This is the CFO's discount-rate lens; it feeds the discount rate in
  [[insurer-valuation]].
---

# P&C insurer cost of capital

What return must this insurer earn to create value, and what does its capital cost?
The CFO's lens: **CAPM** cost of equity, **after-tax cost of debt**, the blended
**WACC**, the **capital structure** (including holding-company double leverage), and
the **RAROC / economic-profit** test that says whether a return clears the hurdle. The
helper does the arithmetic; you supply the market inputs and the structural read.

Full theory — CAPM and insurer betas, the WACC weights, double leverage, RAROC vs
EVA, frictional capital cost — is in [reference.md](reference.md).

## Where the data lives

- **`prices`** — the price store backs **market-cap** (price × shares) for the WACC
  equity weight and the **equity beta** (insurer daily returns regressed on SPY, via
  `--compute-beta`).
- **`insurer_xbrl_facts`** — `equity.common_equity` / `shares_outstanding`,
  `capital_structure.long_term_debt` (debt weight), `segment_results.net_income` (→ ROE).
- **Market inputs you supply:** risk-free rate, ERP, pretax cost of debt, tax rate, and
  (for RAROC) economic capital + risk-adjusted earnings — these are exogenous, not in
  the warehouse.

## Procedure

```bash
python3 .Codex/skills/cost-of-capital/scripts/cost_of_capital.py \
    --db data/state.db --insurer ALL --compute-beta \
    --risk-free 0.04 --erp 0.05 --pretax-cost-of-debt 0.05 --tax-rate 0.21
# or fully explicit — see the script header / --demo.
```

Read:
- **Ke (CAPM)** = rf + β·ERP — the equity hurdle.
- **after-tax Kd** = pretax Kd · (1 − tax).
- **WACC** = (E/V)·Ke + (D/V)·Kd, on **market** weights (E = market cap, D = debt).
- **capital structure**: debt/capital, debt/equity, and **double leverage** = equity in
  subsidiaries / parent equity.
- **economic-profit spread** = ROE − Ke (× book = EVA); **RAROC** = risk-adjusted
  earnings / economic capital vs the Ke hurdle.

`--demo` runs the verified worked example; `--stdin` takes JSON.

## Interpreting the result (judgment, not arithmetic)

- **Ke, not WACC, is usually the right hurdle for an insurer.** Insurers are
  equity-funded businesses — debt is a small slice and statutory capital is equity.
  Value creation is **ROE > Ke**, and most valuation discounts at Ke (see
  [[insurer-valuation]]). Use WACC only for whole-firm / project cash flows that include
  the debt tax shield.
- **Insurer betas cluster near 1** but vary by line — a cat-exposed property writer has
  more market-correlated tail risk than a stable personal-auto book; a regulated rate
  environment dampens beta. A β computed from a short window is noisy — say the lookback
  and flag thin data.
- **Double leverage is a holding-company red flag.** When the parent's equity in its
  insurance subs exceeds its own equity (> 1.0×), the gap is funded with **holdco debt**
  serviced only by **dividends upstreamed from regulated subs** — which are capacity-
  constrained. High double leverage + tight dividend capacity is a liquidity squeeze in
  the making → hand off to [[insurer-liquidity]].
- **Economic profit is the bridge to valuation.** `(ROE − Ke)·book` (EVA) is the same
  excess return that residual-income valuation capitalizes — a carrier with a positive,
  durable spread *should* trade above book; the size of the spread sets how far above.
- **RAROC allocates capital to risk.** It's how a CFO decides which lines earn their
  keep — a line returning 16% on its economic capital against a 9% hurdle creates value;
  one at 7% destroys it even if it's "profitable" on an accounting basis. The economic-
  capital denominator (not GAAP or even statutory capital) is the honest base.

## Output discipline

Lead with Ke and the economic-profit spread, then the structure caveat. E.g. *"ALL's
cost of equity is ~9.0% (β 1.0, 4% rf, 5% ERP); WACC 8.5% on a 10%-debt structure. It
earns a 14% ROE → a +5pt economic-profit spread (~$1.25B EVA), so it creates value and
should trade above book. Watch the 1.12× double leverage — holdco debt leans on
upstream dividends; check dividend capacity before assuming the spread is safe."*
