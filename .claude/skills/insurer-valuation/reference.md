# P&C insurer valuation — reference

Deeper backing for [SKILL.md](SKILL.md): why book value anchors the valuation, the
clean-surplus identity that makes the three models agree, the fade and normalization,
sum-of-the-parts, and the worked example the script's `--demo` reproduces.

## 1. Why P/B–ROE, not EBITDA multiples

For an industrial, book value is an accounting residual. For an insurer, equity *is*
the risk capital that backs the policies, and ROE is the return on it — both are
economically real. So the value is the capitalized **excess return** over the cost of
that capital:

```
P/B = 1  ⟺  ROE = Ke           (earning exactly the cost of equity → worth book)
P/B > 1  ⟺  ROE > Ke           (value created)
P/B < 1  ⟺  ROE < Ke           (value destroyed)
```

## 2. The three models and why they agree (clean surplus)

Under clean-surplus accounting (`BVₜ = BVₜ₋₁ + Eₜ − Dₜ`), constant `ROE` and constant
growth `g = b·ROE` (b = retention), all three reduce to the **same** closed form:

```
justified P/B      = (ROE − g) / (Ke − g)
residual income    V = BV₀ + Σ (ROEₜ − Ke)·BVₜ₋₁ / (1+Ke)ᵗ  → BV₀·(ROE−g)/(Ke−g)
DDM (Gordon)       V = D₁/(Ke − g),  D₁ = BV₀·(ROE − g)      → BV₀·(ROE−g)/(Ke−g)
```

The script computes residual income as an **explicit** multi-year forecast plus a
Gordon terminal; with constant ROE it lands exactly on the closed form — that
self-consistency is the verification. The value of showing all three: when you input a
*non*-constant path (a fading ROE, an inconsistent payout), they diverge and the RI
model is the one to trust because it values the excess return directly and is least
sensitive to the terminal assumption (most of the value is already in book).

## 3. Residual income with a fade

The realistic case: a hard-market ROE fades toward a normalized level. The script
takes `--terminal-roe` and linearly fades `ROEₜ` over the horizon, accumulating
`(ROEₜ − Ke)·BVₜ₋₁` discounted, then a terminal continuing at the terminal spread.
Because `V = BV₀ + PV(excess returns)`, a carrier that will only ever earn its cost of
capital is worth book — no more — regardless of size or growth. This is the disciplined
antidote to capitalizing a cyclical peak.

## 4. Operating ROE — the quality split (DuPont)

Total ROE mixes durable underwriting + investment income with volatile realized gains
and AOCI. Decompose:

```
pretax operating income   = underwriting profit + net investment income
after-tax operating income = pretax operating × (1 − tax)
operating ROE = after-tax operating income / book
non-operating ROE = total ROE − operating ROE   (realized gains, one-offs)
```

Capitalize the **operating** ROE; treat the non-operating slice as non-recurring. A
carrier whose reported ROE beats peers only because of realized gains is lower quality
than the headline. (A fuller insurer DuPont: `ROE = [UW margin·(premium/equity) +
investment yield·(investments/equity)]·(1−tax)` — the investment-leverage term is why
float matters; see [[insurance-investment-portfolio]].)

## 5. Peer P/B-on-ROE regression

Across a peer set, fit `P/B = a + b·ROE` by OLS. The carrier's **residual** from the
line — not its absolute multiple — is the relative-value signal: above the line = rich
for its returns, below = cheap. The slope `b` is the market's current price on a point
of ROE. This neutralizes the "high-quality compounder looks expensive" trap: PGR's
2.5× is cheap *relative to the line* if its ROE sits far enough right.

## 6. Sum-of-the-parts and normalized earnings

- **Sum-of-the-parts:** value the underwriting operation (normalized after-tax UW
  profit ÷ Ke, or a target combined-ratio earnings power) plus the investment portfolio
  (the bond book near fair value, or float × sustainable spread) separately. Useful when
  the market is mispricing one leg — e.g. crediting zero for a profitable specialty book.
- **Normalized earnings:** replace the reported combined ratio with a mid-cycle, normal-
  cat-load, ex-development figure ([[combined-ratio-bridge]]), recompute normalized ROE,
  then capitalize. The single biggest valuation error in insurance is treating a
  hard-market peak ROE (inflated by releases + light cats) as the through-the-cycle rate.

## 7. Worked example (the script's `--demo`)

Inputs: book 25,000 ($M) · 250M shares (BVPS $100) · net income 3,500 (ROE 14.0%) ·
Ke 9.0% · g 4.0% · price $180. Operating: UW profit 800, NII 3,400, tax 21%.

- justified P/B = (0.14 − 0.04)/(0.09 − 0.04) = 0.10/0.05 = **2.0×**.
- residual income = 25,000 + PV excess returns = **50,000** (implied P/B 2.0×).
- DDM: D₁ = 25,000·(0.14−0.04) = 2,500; V = 2,500/0.05 = **50,000** (payout 71.4%).
  All three agree → fair value $200/share.
- operating ROE = (800 + 3,400)·0.79 / 25,000 = **13.3%** vs total 14.0% → 0.7 pt is
  realized gains (lower quality).
- market P/B 180/100 = **1.8×** → upside to justified 200/180 − 1 = **11.1%**; P/E
  180/(3,500/250) = **12.9×**.
- peer line (A 1.5/11%, B 2.2/15%, C 1.8/13%, D 2.6/17%): `P/B = −0.56 + 18.5·ROE` →
  fair **2.02×** at 14% ROE, ~12.5% upside — corroborates the absolute model.

## 8. Pitfalls checklist

- **g ≥ Ke breaks every model** (negative denominator) — the script exits. g also can't
  exceed sustainable `b·ROE`.
- **Use GAAP book for P/B**, statutory surplus only for capital work — don't cross them.
  AOCI swings book quarter to quarter (rate moves); a tangible or ex-AOCI book is often
  the more stable anchor — note which you used.
- **Per-share needs a clean share count** — buybacks and the cover-page vs weighted-
  average distinction matter; the warehouse `equity` dataset carries both
  (`shares_outstanding`, `diluted_wavg_shares`).
- **Cyclical peak ≠ perpetual.** Always normalize before capitalizing.
- **Mutuals have no P/B** (no traded equity) — value them on statutory surplus growth /
  combined ratio, not a stock multiple.

## 9. How this maps to the warehouse

- `insurer_xbrl_facts` `equity` (book, shares) + `segment_results.net_income` (ROE) +
  `prices.close` (market) drive every multiple.
- `Ke` comes from [[cost-of-capital]]; normalized earnings from
  [[combined-ratio-bridge]] + [[reserving-chain-ladder]]; the investment-leverage term
  from [[insurance-investment-portfolio]].
- The advisory alpha-engine `return_forecasts` is a *separate*, model-based forward-
  return view — keep it distinct from this fundamental valuation; they triangulate but
  one never feeds the other.
