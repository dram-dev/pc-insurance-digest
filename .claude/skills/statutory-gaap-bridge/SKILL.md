---
name: statutory-gaap-bridge
description: >-
  Reconcile a P&C insurer's GAAP shareholders' equity to its NAIC statutory
  surplus, and decompose the year-over-year change in statutory surplus. Use when
  a question involves statutory vs GAAP capital, statutory surplus, admitted vs
  non-admitted assets, why surplus differs from book equity, DAC / AOCI / deferred
  tax accounting differences, or the "change in capital and surplus account." STAT
  surplus — not GAAP equity — is what RBC, dividend capacity and leverage ratios
  divide by, so getting this bridge right is the foundation of every capital,
  solvency and liquidity read.
---

# GAAP equity ↔ statutory surplus bridge

A P&C insurer keeps two sets of books: **GAAP** (the 10-K, accrual, going-concern)
and **Statutory / SAP** (the NAIC annual statement, conservative, liquidation-biased).
Their capital figures — GAAP **shareholders' equity** vs statutory **surplus** —
are *not* the same number, and the gap is structural, not noise. Statutory surplus
is the denominator of RBC, premium-to-surplus leverage and dividend capacity, so you
must be able to move between the two. This skill is the method; the helper does the
waterfall arithmetic and you supply the judgment.

For the full theory (every reconciling item, the sign conventions, SSAP references,
and the worked example) read [reference.md](reference.md). This file is the operating
procedure.

## Where the data lives

The warehouse has *some* of the bridge, never all of it:
- **`insurer_xbrl_facts`** — GAAP side. `dataset='equity'` (`common_equity`,
  `shares_outstanding`), `dataset='aoci'` (`oci_net`), `dataset='dac'`
  (`dac_balance`). These are the items that drive most of the gap.
- **`statutory_facts`** — `dataset='surplus'` is the *reported* statutory surplus
  (mostly the big mutuals + a few stock carriers), your cross-check target.
- The remaining reconciling items (non-admitted DTA, goodwill/intangibles, provision
  for reinsurance) are **not ingested** — read them from the carrier's Five-Year
  Statutory exhibit / Schedule, and pass them in.

## Procedure

1. **Bridge mode** (GAAP equity → implied statutory surplus):
   ```bash
   python3 .claude/skills/statutory-gaap-bridge/scripts/statutory_gaap_bridge.py \
       --db data/state.db --insurer TRV \
       --goodwill-intangibles 1500 --non-admitted-dta 400   # supply the gaps
   ```
   `--db` pulls GAAP equity / AOCI / DAC / reported surplus where ingested; CLI flags
   fill or override. Or pass everything explicitly / via `--stdin`, and `--demo` for
   the verified worked example. All figures USD millions.

2. **Read the waterfall, in order:** GAAP equity → −AOCI (reverse AFS bonds to
   amortized cost) → −DAC → −goodwill/intangibles → −non-admitted DTA → −provision
   for reinsurance → **implied statutory surplus**. The script flags any item it had
   to treat as 0 (not supplied) and, if you gave `--reported-surplus`, the
   **unexplained residual**.

3. **Change-in-surplus mode** (`--change`): decompose the YoY move —
   ```bash
   python3 …/statutory_gaap_bridge.py --change --begin-surplus 20000 \
       --stat-net-income 2400 --unrealized-change 300 --dividends -1200 …
   ```
   Reports each driver's share of the total change. This is the statutory analogue of
   a retained-earnings rollforward and the cleanest read of *how* capital moved.

## Interpreting the result (judgment, not arithmetic)

- **DAC is the biggest structural gap.** GAAP capitalizes acquisition costs and
  amortizes them as an asset; SAP expenses them immediately. So statutory surplus is
  lower by ~the DAC balance — a growing book *depresses* statutory surplus relative
  to GAAP exactly when the company is writing the most business. Never read a falling
  surplus-to-equity ratio as deterioration without checking DAC growth first.
- **AOCI is the rate-sensitive gap.** GAAP marks AFS bonds to fair value with the
  unrealized gain/loss in AOCI; SAP holds them at amortized cost. Post-2022, GAAP
  equity carries large unrealized **losses** that statutory surplus never recognized
  → surplus > the AOCI-depressed GAAP equity. The sign flips with rates.
- **A large unexplained residual means your reconciling items are incomplete** — say
  so, don't force the number. The point of the bridge is to attribute the gap, and an
  honest "≈$X unexplained, likely non-admitted DTA + Schedule F" beats false precision.
- **Direction matters in change mode.** Surplus up on *net income* is earned capital;
  surplus up on *unrealized gains* is mark-to-market and can reverse; surplus up on
  *paid-in capital* is a raise (dilutive / a tell that organic capital was short).
  Reserve strengthening shows up *inside* statutory net income, not as its own line.

## Output discipline

Lead with the two capital numbers and the dominant driver of the gap, then the
caveat. E.g. *"TRV GAAP equity $25.0B vs implied statutory surplus $21.2B — a $3.8B
gap that is ~80% DAC ($3.0B, non-admitted) and a $1.2B AOCI bond loss SAP ignores;
$0.5B unexplained, likely non-admitted DTA + Schedule F provision. Use the $21.2B
surplus, not the $25.0B equity, for RBC and dividend-capacity work."* Hand the SQL or
the supplied inputs back so the user can refine the missing items.
