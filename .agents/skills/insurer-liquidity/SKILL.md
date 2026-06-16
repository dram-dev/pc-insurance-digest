---
name: insurer-liquidity
description: >-
  Liquidity and treasury management for a P&C insurer — statutory dividend capacity
  (the regulated sub's cap on cash it can upstream), holding-company vs operating-
  company liquidity, fixed-charge / interest coverage and holdco cash runway, the
  sources-and-uses view, and a catastrophe liquidity stress (liquid assets +
  contingent capital vs a 1-in-100 net cat). Use when a question involves whether a
  carrier can pay its dividends / service its debt, holding-company liquidity,
  dividend capacity, cash runway, coverage ratios, refinancing risk, or the cash
  needed to pay a large catastrophe. Cash is trapped in regulated subs — solvency and
  liquidity are different questions.
---

# P&C insurer liquidity & treasury

A P&C insurer can be well-capitalized yet liquidity-constrained: the cash sits in
**regulated operating subsidiaries** and can only reach the **holding company** as
**dividends**, which states cap. So the holdco — which owes the interest and the
shareholder dividend — depends on a regulated, throttled upstream flow. This skill
computes statutory **dividend capacity**, holdco **coverage & runway**, and a
**catastrophe liquidity** stress. The helper does the arithmetic; you supply the
state-law and structural judgment.

Full theory — the HoldCo/OpCo structure, the dividend-capacity rules, coverage ratios,
contingent capital, debt laddering — is in [reference.md](reference.md).

## Where the data lives

- **`statutory_facts.surplus`** — the base for dividend capacity.
- **`insurer_xbrl_facts`** — `liquidity.cash_and_equivalents` (holdco liquid proxy),
  `liquidity.dividends_paid` (common dividend), `capital_structure.interest_expense`
  (debt service), `segment_results.net_income` (a GAAP proxy for the statutory-NI
  capacity input — flagged, since the rule is on *statutory* NI).
- **Not ingested:** prior-year statutory net income (vs GAAP), the holdco/opex split,
  contingent-capital facilities (FHLB, revolver, cat bonds), and the cat PML — supply
  these from the 10-K / statutory filing.

## Procedure

```bash
python3 .Codex/skills/insurer-liquidity/scripts/insurer_liquidity.py \
    --prior-surplus 6000 --prior-stat-net-income 700 --holdco-liquid 1500 \
    --interest 200 --common-dividends 900 --holdco-opex 50 \
    --holdco-investment-income 80 \
    --cat-net-loss 2500 --liquid-investments 8000 --contingent-capital 1500
# warehouse-assisted:
python3 …/insurer_liquidity.py --db data/state.db --insurer HIG --cat-net-loss 2500
```

Read the three blocks:
- **Dividend capacity** = greater of 10%·surplus or prior-year statutory NI (NAIC
  model; some states use the lesser / net investment income). Above it = an
  *extraordinary* dividend needing regulator approval.
- **HoldCo coverage**: sources (upstream dividends + holdco investment income) vs uses
  (interest + common dividend + opex); **interest coverage**, total-obligation
  coverage, **net holdco cash flow**, and the **cash runway** if there's a drain.
- **Cat liquidity**: a 1-in-100 net cat vs liquid investments + contingent capital.

`--demo` runs the verified worked example; `--stdin` takes JSON.

## Interpreting the result (judgment, not arithmetic)

- **Solvency ≠ liquidity.** A group can pass RBC ([[insurer-capital-adequacy]]) with
  ample surplus and still have a holdco that can't fund its dividend, because the
  surplus is *trapped* in the subs. Always separate "is there enough capital" from "can
  the cash get to where the obligation is."
- **Dividend capacity is the binding constraint.** It caps the organic upstream flow.
  When the holdco's fixed charges (interest) plus the shareholder dividend exceed
  capacity + holdco income, the gap is funded from holdco cash — finite (the runway) —
  or by raising sub dividends (which depletes statutory surplus and pressures RBC) or
  cutting the payout. This loop is the core holdco-liquidity story.
- **Interest coverage vs total coverage.** Debt service is usually well covered
  (interest is small); the strain is the *common dividend*, which is discretionary but
  sticky. A 3.9× interest coverage with a 0.7× total-obligation coverage means the debt
  is safe but the dividend is being part-funded from cash — sustainable for a few years,
  not indefinitely.
- **Double leverage amplifies this** — holdco debt raised to down-stream sub equity must
  be serviced from those same capped sub dividends. Read alongside [[cost-of-capital]]'s
  double-leverage flag.
- **Cat liquidity is a timing problem, not a solvency one.** Even a well-reserved cat
  must be *paid in cash* fast; the carrier may need to liquidate investments (realizing
  AOCI losses — [[insurance-investment-portfolio]]) or draw FHLB/revolver/cat-bond
  proceeds. Coverage < 1× of a 1-in-100 net loss is a real liquidity gap even if surplus
  is fine.

## Output discipline

Lead with the binding constraint and the runway. E.g. *"HIG's regulated subs can upstream
~$700M of ordinary dividends; with $80M holdco income that's $780M of sources against
$1,150M of uses ($200M interest + $900M common dividend + opex). Interest is well
covered (3.9×), but the dividend is part-funded from the $1.5B holdco cash → ~4-year
runway. Cat liquidity is ample (3.8× a 1-in-100 net loss). The watch item is the payout,
not solvency."*
