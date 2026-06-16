#!/usr/bin/env python3
"""P&C insurer investment portfolio — float & cost of float, book vs new-money yield,
investment leverage, and the ALM duration gap + AOCI rate shock. Pure stdlib.

The asset side of an insurer is unlike a generic portfolio: it is funded by
policyholder **float**, constrained to **match the liability duration**, and its
unrealized gain/loss runs straight through **AOCI** into GAAP equity. This computes
the four reads an insurance investment analyst actually uses.

  python investment_portfolio.py --loss-reserves 30000 --unearned-premium 8000 \
      --recoverables 5000 --dac 2500 --uw-profit 800 \
      --invested-assets 60000 --nii 2400 --new-money-yield 0.055 --equity 25000 \
      --asset-duration 4.5 --liability-duration 3.0 --bond-market-value 50000 \
      --rate-shock 0.01 --liability-market-value 30500

  # From the warehouse (float, yields, leverage; durations + shock you supply):
  python investment_portfolio.py --db data/state.db --insurer CB \
      --asset-duration 4.5 --liability-duration 3.0 --rate-shock 0.01

  # --stdin for JSON, --demo for the verified worked example.

$ in USD millions; durations in years; yields & rate shock are decimals. Outputs a
human report; --format json for structured.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.2f}%".replace("+0.00%", "0.00%").replace("-0.00%", "0.00%")


def _bps(x: float | None) -> str:
    return "—" if x is None else f"{x * 10000:+.0f} bps"


def float_block(p: dict) -> dict | None:
    res, upr = p.get("loss_reserves"), p.get("unearned_premium")
    if res is None and upr is None:
        return None
    flt = (res or 0) + (upr or 0) - (p.get("recoverables") or 0) - (p.get("dac") or 0) \
        - (p.get("agents_balances") or 0)
    out = {"float": flt, "components": {
        "loss_and_lae_reserves": res, "unearned_premium": upr,
        "less_recoverables": p.get("recoverables"), "less_dac": p.get("dac"),
        "less_agents_balances": p.get("agents_balances")}}
    uw = p.get("uw_profit")
    if uw is not None and flt:
        out["underwriting_profit"] = uw
        out["cost_of_float"] = -uw / flt   # negative = paid to hold the money
    return out


def yield_block(p: dict) -> dict | None:
    ia, nii = p.get("invested_assets"), p.get("nii")
    if not ia or nii is None:
        return None
    book_yield = nii / ia
    out = {"invested_assets": ia, "nii": nii, "book_yield": book_yield}
    nmy = p.get("new_money_yield")
    if nmy is not None:
        out["new_money_yield"] = nmy
        out["reinvestment_drift"] = nmy - book_yield
    eq = p.get("equity")
    if eq:
        out["investment_leverage"] = ia / eq
        out["nii_contribution_to_pretax_roe"] = nii / eq   # = book_yield × leverage
    return out


def alm_block(p: dict) -> dict | None:
    da, dl = p.get("asset_duration"), p.get("liability_duration")
    if da is None and dl is None:
        return None
    out = {"asset_duration": da, "liability_duration": dl}
    if da is not None and dl is not None:
        out["duration_gap"] = da - dl
    shock = p.get("rate_shock")
    bond_mv = p.get("bond_market_value") or p.get("invested_assets")
    if shock is not None and da is not None and bond_mv:
        aoci_hit = -da * shock * bond_mv          # GAAP AFS unrealized P&L → AOCI
        out["rate_shock"] = shock
        out["bond_market_value"] = bond_mv
        out["aoci_hit"] = aoci_hit
        eq = p.get("equity")
        if eq:
            out["aoci_hit_pct_of_equity"] = aoci_hit / eq
        lmv, dlv = p.get("liability_market_value"), dl
        if lmv is not None and dlv is not None:
            # economic surplus change if the liability also revalues (P&C GAAP does
            # NOT mark reserves, so GAAP equity moves MORE than economic surplus).
            out["economic_surplus_change"] = -(da * bond_mv - dlv * lmv) * shock
    return out


def assess(p: dict) -> dict:
    out = {"float": float_block(p), "yields": yield_block(p), "alm": alm_block(p),
           "warnings": []}
    if not any(out[k] for k in ("float", "yields", "alm")):
        sys.exit("investment_portfolio: not enough inputs for any block. Provide "
                 "reserves/UPR (float), invested assets + NII (yield), or durations (ALM).")
    f = out["float"]
    if f and f.get("cost_of_float") is not None and f["cost_of_float"] < 0:
        out["warnings"].append("cost of float is NEGATIVE — the carrier is paid to hold "
                               "policyholder money (underwriting profit funds the float).")
    a = out["alm"]
    if a and a.get("duration_gap") is not None and abs(a["duration_gap"]) > 2.0:
        out["warnings"].append(f"duration gap {a['duration_gap']:+.1f}y is large — "
                               "material ALM mismatch / rate exposure.")
    return out


def from_db(db_path: Path, insurer: str) -> dict:
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def fact(dataset, field):
            row = con.execute(
                "SELECT SUM(value) FROM insurer_xbrl_facts WHERE insurer=? AND dataset=? "
                "AND field=? AND accident_year IS NULL "
                "AND period_end=(SELECT MAX(period_end) FROM insurer_xbrl_facts "
                "WHERE insurer=? AND dataset=? AND field=?)",
                (insurer, dataset, field, insurer, dataset, field)).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        payload = {
            "loss_reserves": fact("unpaid_claims", "liability_net"),
            "unearned_premium": fact("premiums", "unearned_premiums"),
            "recoverables": fact("reinsurance", "recoverable_unpaid"),
            "dac": fact("dac", "dac_balance"),
            "invested_assets": fact("investment_portfolio", "investments_fair_value"),
            "bond_market_value": fact("investment_portfolio", "afs_debt_securities"),
            "nii": fact("investment_income", "net_investment_income"),
            "equity": fact("equity", "common_equity"),
        }
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO = {"loss_reserves": 30000, "unearned_premium": 8000, "recoverables": 5000,
         "dac": 2500, "uw_profit": 800, "invested_assets": 60000, "nii": 2400,
         "new_money_yield": 0.055, "equity": 25000, "asset_duration": 4.5,
         "liability_duration": 3.0, "bond_market_value": 50000, "rate_shock": 0.01,
         "liability_market_value": 30500}


def render(o: dict) -> str:
    L = ["INVESTMENT PORTFOLIO"]
    f = o["float"]
    if f:
        L.append(f"  ── Float ──  {f['float']:,.0f}")
        if f.get("cost_of_float") is not None:
            L.append(f"    underwriting profit {f['underwriting_profit']:,.0f}  → cost of "
                     f"float {_pct(f['cost_of_float'])}  (negative = paid to hold)")
    y = o["yields"]
    if y:
        L.append("  ── Yield / leverage ──")
        L.append(f"    book yield {_pct(y['book_yield'])} (NII {y['nii']:,.0f} / "
                 f"invested {y['invested_assets']:,.0f})")
        if "new_money_yield" in y:
            L.append(f"    new-money yield {_pct(y['new_money_yield'])}  → reinvestment "
                     f"drift {_bps(y['reinvestment_drift'])}")
        if "investment_leverage" in y:
            L.append(f"    investment leverage {y['investment_leverage']:.2f}×  → NII adds "
                     f"{_pct(y['nii_contribution_to_pretax_roe'])} to pretax ROE")
    a = o["alm"]
    if a:
        L.append("  ── ALM / rate risk ──")
        if a.get("duration_gap") is not None:
            L.append(f"    asset dur {a['asset_duration']:.1f}y  liability dur "
                     f"{a['liability_duration']:.1f}y  → gap {a['duration_gap']:+.1f}y")
        if "aoci_hit" in a:
            L.append(f"    +{a['rate_shock']*10000:.0f}bps shock → AOCI hit "
                     f"{a['aoci_hit']:,.0f}" + (f" ({_pct(a['aoci_hit_pct_of_equity'])} of "
                     f"equity)" if "aoci_hit_pct_of_equity" in a else ""))
            if "economic_surplus_change" in a:
                L.append(f"    economic surplus change (liabilities revalue) "
                         f"{a['economic_surplus_change']:,.0f}  — GAAP overstates the hit "
                         "because reserves aren't marked")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="P&C insurer investment portfolio / ALM.")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    for d in ("loss_reserves", "unearned_premium", "recoverables", "dac",
              "agents_balances", "uw_profit", "invested_assets", "nii",
              "new_money_yield", "equity", "asset_duration", "liability_duration",
              "bond_market_value", "rate_shock", "liability_market_value"):
        ap.add_argument(f"--{d.replace('_', '-')}", type=float)
    args = ap.parse_args()

    skip = ("db", "insurer", "stdin", "demo", "format")
    if args.demo:
        p = dict(_DEMO)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"investment_portfolio: bad JSON on stdin ({e}).")
    elif args.db:
        if not args.insurer:
            sys.exit("investment_portfolio: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in skip:
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args)
             if getattr(args, d) is not None and d not in skip}
        if not p:
            sys.exit("investment_portfolio: no inputs. Pass flags, --stdin, --db, or --demo.")

    out = assess(p)
    if args.insurer:
        out["insurer"] = args.insurer
    print(json.dumps(out, indent=2) if args.format == "json" else render(out))


if __name__ == "__main__":
    main()
