#!/usr/bin/env python3
"""P&C insurer liquidity & treasury — statutory dividend capacity (the holdco's only
organic cash source), holding-company coverage & cash runway, and a catastrophe
liquidity stress. Pure stdlib (no numpy/pandas).

The distinctive insurer liquidity problem is the HOLDCO/OPCO split: cash sits in
regulated operating subs and can only move up to the parent as DIVIDENDS, which states
cap. So a holdco can be asset-rich yet cash-poor. This computes the three reads a
treasurer/CFO uses.

  python insurer_liquidity.py --prior-surplus 6000 --prior-stat-net-income 700 \
      --holdco-liquid 1500 --interest 200 --common-dividends 900 --holdco-opex 50 \
      --holdco-investment-income 80 \
      --cat-net-loss 2500 --liquid-investments 8000 --contingent-capital 1500

  # From the warehouse (surplus / holdco cash / dividends / interest):
  python insurer_liquidity.py --db data/state.db --insurer HIG --cat-net-loss 2500

  # --stdin for JSON, --demo for the verified worked example.

$ figures in USD millions. Outputs a human report; --format json for structured.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _x(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f}×"


def dividend_capacity(p: dict) -> dict | None:
    surplus, ni = p.get("prior_surplus"), p.get("prior_stat_net_income")
    if surplus is None and ni is None:
        return None
    # NAIC model: an "extraordinary" dividend exceeds the GREATER of 10% of surplus or
    # prior-year statutory net income; up to that is "ordinary" (no prior approval).
    ten_pct = 0.10 * surplus if surplus is not None else None
    parts = [v for v in (ten_pct, ni) if v is not None]
    capacity = max(parts) if parts else None
    out = {"ten_pct_of_surplus": ten_pct, "prior_stat_net_income": ni,
           "ordinary_dividend_capacity": capacity,
           "basis": "greater of 10%·surplus or prior-yr statutory NI (NAIC model; "
                    "some states use the LESSER or net investment income)"}
    return out


def holdco_coverage(p: dict) -> dict | None:
    interest = p.get("interest")
    common = p.get("common_dividends")
    opex = p.get("holdco_opex", 0.0) or 0.0
    upstream = p.get("upstream_dividends")
    if upstream is None:                       # default to the ordinary capacity
        dc = dividend_capacity(p)
        upstream = dc["ordinary_dividend_capacity"] if dc else None
    inv_inc = p.get("holdco_investment_income", 0.0) or 0.0
    if interest is None and common is None:
        return None
    sources = (upstream or 0.0) + inv_inc
    uses = (interest or 0.0) + (common or 0.0) + opex
    out = {"holdco_sources": sources, "holdco_uses": uses,
           "upstream_dividends": upstream, "holdco_investment_income": inv_inc,
           "interest": interest, "common_dividends": common, "holdco_opex": opex,
           "net_holdco_cashflow": sources - uses}
    if interest:
        out["interest_coverage"] = sources / interest
    if uses:
        out["total_obligation_coverage"] = sources / uses
    shortfall = uses - sources
    liquid = p.get("holdco_liquid")
    if liquid is not None:
        out["holdco_liquid"] = liquid
        if shortfall > 0:
            out["cash_runway_years"] = liquid / shortfall
        else:
            out["cash_runway_years"] = None      # self-funding, no drain
    return out


def cat_liquidity(p: dict) -> dict | None:
    cat = p.get("cat_net_loss")
    if cat is None:
        return None
    liquid = p.get("liquid_investments", 0.0) or 0.0
    contingent = p.get("contingent_capital", 0.0) or 0.0
    avail = liquid + contingent
    return {"cat_net_loss": cat, "liquid_investments": liquid,
            "contingent_capital": contingent, "available_liquidity": avail,
            "coverage": (avail / cat) if cat else None}


def assess(p: dict) -> dict:
    out = {"dividend_capacity": dividend_capacity(p), "holdco": holdco_coverage(p),
           "cat_liquidity": cat_liquidity(p), "warnings": []}
    if not any(out[k] for k in ("dividend_capacity", "holdco", "cat_liquidity")):
        sys.exit("insurer_liquidity: not enough inputs. Provide surplus/NI (dividend "
                 "capacity), holdco interest/dividends (coverage), or a cat loss + "
                 "liquidity (stress).")
    h = out["holdco"]
    if h:
        if h.get("interest_coverage") is not None and h["interest_coverage"] < 2.0:
            out["warnings"].append(f"interest coverage {h['interest_coverage']:.1f}× < 2× "
                                   "— thin debt-service cushion at the holdco.")
        if h.get("net_holdco_cashflow", 0) < 0:
            rw = h.get("cash_runway_years")
            out["warnings"].append(
                "holdco uses exceed organic sources — the common dividend is partly "
                "funded from holdco cash" + (f" (~{rw:.1f}y runway at this pace)"
                                             if rw else "") + "; not sustainable without "
                "raising sub dividends or cutting the payout.")
    c = out["cat_liquidity"]
    if c and c.get("coverage") is not None and c["coverage"] < 1.0:
        out["warnings"].append(f"cat liquidity coverage {c['coverage']:.1f}× < 1× — a "
                               "1-in-100 cat would exhaust liquid + contingent capital.")
    return out


def from_db(db_path: Path, insurer: str) -> dict:
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def fact(dataset, field):
            row = con.execute(
                "SELECT value FROM insurer_xbrl_facts WHERE insurer=? AND dataset=? "
                "AND field=? AND segment IS NULL AND product IS NULL "
                "ORDER BY period_end DESC, as_of DESC LIMIT 1",
                (insurer, dataset, field)).fetchone()
            return float(row[0]) if row else None
        srow = con.execute("SELECT value FROM statutory_facts WHERE insurer=? AND "
                           "dataset='surplus' ORDER BY period DESC LIMIT 1",
                           (insurer,)).fetchone()
        payload = {
            "prior_surplus": float(srow[0]) if srow else None,
            "prior_stat_net_income": fact("segment_results", "net_income"),  # GAAP proxy
            "holdco_liquid": fact("liquidity", "cash_and_equivalents"),
            "common_dividends": fact("liquidity", "dividends_paid"),
            "interest": fact("capital_structure", "interest_expense"),
        }
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO = {"prior_surplus": 6000, "prior_stat_net_income": 700, "holdco_liquid": 1500,
         "interest": 200, "common_dividends": 900, "holdco_opex": 50,
         "holdco_investment_income": 80, "cat_net_loss": 2500,
         "liquid_investments": 8000, "contingent_capital": 1500}


def render(o: dict) -> str:
    L = ["INSURER LIQUIDITY"]
    d = o["dividend_capacity"]
    if d:
        L.append("  ── Statutory dividend capacity (OpCo → HoldCo) ──")
        L.append(f"    10%·surplus {(_fmt(d['ten_pct_of_surplus']))}   prior stat NI "
                 f"{_fmt(d['prior_stat_net_income'])}   → ordinary capacity "
                 f"{_fmt(d['ordinary_dividend_capacity'])}")
    h = o["holdco"]
    if h:
        L.append("  ── HoldCo coverage & runway ──")
        L.append(f"    sources {_fmt(h['holdco_sources'])} (upstream "
                 f"{_fmt(h['upstream_dividends'])} + inv inc {_fmt(h['holdco_investment_income'])})"
                 f"  vs uses {_fmt(h['holdco_uses'])} (int {_fmt(h['interest'])} + "
                 f"common div {_fmt(h['common_dividends'])} + opex {_fmt(h['holdco_opex'])})")
        L.append(f"    interest coverage {_x(h.get('interest_coverage'))}   total-"
                 f"obligation coverage {_x(h.get('total_obligation_coverage'))}   "
                 f"net holdco CF {_fmt(h['net_holdco_cashflow'])}")
        if h.get("cash_runway_years") is not None:
            L.append(f"    holdco liquid {_fmt(h.get('holdco_liquid'))} → cash runway "
                     f"{h['cash_runway_years']:.1f}y at this drain")
    c = o["cat_liquidity"]
    if c:
        L.append("  ── Catastrophe liquidity stress ──")
        L.append(f"    1-in-100 net cat {_fmt(c['cat_net_loss'])}  vs available "
                 f"{_fmt(c['available_liquidity'])} (liquid {_fmt(c['liquid_investments'])} "
                 f"+ contingent {_fmt(c['contingent_capital'])})  → coverage {_x(c['coverage'])}")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="P&C insurer liquidity (dividend capacity / coverage / cat).")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    for d in ("prior_surplus", "prior_stat_net_income", "holdco_liquid", "interest",
              "common_dividends", "holdco_opex", "upstream_dividends",
              "holdco_investment_income", "cat_net_loss", "liquid_investments",
              "contingent_capital"):
        ap.add_argument(f"--{d.replace('_', '-')}", type=float)
    args = ap.parse_args()

    skip = ("db", "insurer", "stdin", "demo", "format")
    if args.demo:
        p = dict(_DEMO)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"insurer_liquidity: bad JSON on stdin ({e}).")
    elif args.db:
        if not args.insurer:
            sys.exit("insurer_liquidity: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in skip:
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args)
             if getattr(args, d) is not None and d not in skip}
        if not p:
            sys.exit("insurer_liquidity: no inputs. Pass flags, --stdin, --db, or --demo.")

    out = assess(p)
    if args.insurer:
        out["insurer"] = args.insurer
    print(json.dumps(out, indent=2) if args.format == "json" else render(out))


if __name__ == "__main__":
    main()
