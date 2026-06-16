# P&C insurer cost of capital — reference

Deeper backing for [SKILL.md](SKILL.md): CAPM and insurer betas, the WACC weights,
capital structure and double leverage, RAROC vs EVA, frictional capital cost, and the
worked example the script's `--demo` reproduces.

## 1. Cost of equity (CAPM)

```
Ke = risk-free rate + β · equity risk premium
```

- **Risk-free** — the 10y Treasury is the usual anchor for a long-lived equity.
- **β (equity beta)** — the stock's sensitivity to the market, `cov(rᵢ, rₘ)/var(rₘ)`,
  estimated from daily/weekly returns vs a broad index (SPY). The script computes it
  from the `prices` store over ~1y. Insurer betas cluster near 1.0 but: cat-exposed
  property/reinsurance run higher tail-correlated risk; stable personal lines and
  heavily regulated books run lower. A levered β can be unlevered/relevered for capital-
  structure changes, but for insurers (low debt) the adjustment is small.
- **ERP** — the market equity risk premium (~4.5–5.5% is the common range).

Ke is the **equity hurdle** and the discount rate for residual-income / DDM valuation
([[insurer-valuation]]). Value is created when **ROE > Ke**.

## 2. After-tax cost of debt and WACC

```
Kd(after-tax) = Kd(pretax) · (1 − tax rate)
WACC = (E/V)·Ke + (D/V)·Kd(after-tax),   V = E + D
```

Use **market** weights — E = market capitalization (price × shares), D ≈ book value of
debt (close to market for most carriers). WACC is the right discount rate for
**whole-firm** cash flows that capture the debt tax shield; for the equity holder's
return and for most insurer valuation, **Ke** is the relevant hurdle because the
business is equity-funded and statutory capital is equity. Surplus notes sit between
debt and equity — count them per the question (debt for leverage, capital for surplus).

## 3. Capital structure & double leverage

```
debt / capital = D / (D + E)
debt / equity  = D / E
double leverage = equity carried in subsidiaries / parent (holdco) equity
```

P&C insurers run **modest financial leverage** (debt/capital typically 15–30%; rating
agencies cap it). The distinctive structural risk is **double leverage**: a holding
company raises debt and down-streams the proceeds as **equity** into its regulated
insurance subs. If the equity the parent carries in subs exceeds the parent's own
equity (> 1.0×), the excess is debt-funded, and the holdco services that debt **only
from dividends the regulated subs are allowed to pay up** — which are statutorily
capped. High double leverage is therefore a *liquidity* constraint, not just a leverage
ratio — connect to [[insurer-liquidity]] (dividend capacity, fixed-charge coverage).

## 4. RAROC and economic profit (EVA)

Two complementary value tests:

```
RAROC = risk-adjusted (after-tax) earnings / economic capital
economic profit (EVA) = (ROE − Ke) · book value
```

- **RAROC** allocates **economic capital** (capital sized to the risk the line actually
  consumes — cat PML, reserve volatility, asset risk; richer than GAAP or even statutory
  capital) and compares the return on it to the Ke hurdle. It's how a CFO ranks lines /
  prices an acquisition / sets a reinsurance buy. A "profitable" line that returns less
  than Ke on its economic capital destroys value.
- **Economic profit / EVA** is the dollar excess return; it equals the numerator that
  residual-income valuation capitalizes (the spread × book). Positive, durable EVA ⇒
  P/B > 1.

## 5. Frictional cost of capital

Holding capital inside an insurer is not free even when invested: it incurs **double
taxation** (the insurer pays tax on the investment income, then the shareholder on the
dividend), **agency/illiquidity** costs, and a regulatory drag. This frictional cost
(a point or two) is why insurers return excess capital (buybacks, special dividends)
rather than hoard it, and why a carrier sitting on capital it can't deploy above its
cost destroys value — the buyback math is just `repurchase below intrinsic P/B`.

## 6. Worked example (the script's `--demo`)

Inputs: rf 4.0% · β 1.0 · ERP 5.0% · pretax Kd 5.0% · tax 21% · equity mktval 45,000 ·
debt 5,000 · ROE 14.0% · book 25,000 · sub equity 28,000 · parent equity 25,000 ·
risk-adjusted earnings 3,318 · economic capital 20,000 ($M).

- **Ke** = 4.0% + 1.0·5.0% = **9.0%**.
- **after-tax Kd** = 5.0%·(1−0.21) = **3.95%**.
- **WACC** = (45/50)·9.0% + (5/50)·3.95% = 8.10% + 0.395% = **8.49%**.
- **structure**: debt/capital 10.0%, debt/equity 11.1%, **double leverage 28,000/25,000
  = 1.12×** (flagged — holdco debt leans on upstream dividends).
- **economic-profit spread** = 14.0% − 9.0% = **5.0%** → EVA = 0.05·25,000 = **1,250**.
- **RAROC** = 3,318/20,000 = **16.6%** > 9.0% hurdle → value-creating.

## 7. Pitfalls checklist

- **Market vs book weights** — WACC uses market-cap equity; book weights distort a
  carrier trading well above or below book.
- **β instability** — a short window or a thinly traded name gives a noisy β; report the
  lookback and prefer a longer window / a peer-derived β when data is thin.
- **Mutuals have no β / market cap** — use a peer Ke or a target ROE; WACC is undefined.
- **Don't double-count surplus notes** — debt for leverage/WACC, capital for surplus;
  pick the treatment the question needs and say so.
- **Economic capital ≠ GAAP/statutory capital** — RAROC's denominator is risk-based;
  using accounting capital understates the hurdle for a high-volatility line.

## 8. How this maps to the warehouse

- `prices` (market cap + β vs SPY), `insurer_xbrl_facts` `equity` (book/shares),
  `capital_structure.long_term_debt` (debt), `segment_results.net_income` (ROE) feed the
  computable parts; market inputs (rf, ERP, Kd) are supplied.
- Ke is the discount rate in [[insurer-valuation]]; the economic-profit spread is the
  same excess return that skill capitalizes.
- Double leverage / coverage hand off to [[insurer-liquidity]]; the investment-income-
  on-capital term of RAROC ties to [[insurance-investment-portfolio]].
