#!/usr/bin/env python3
"""Ceded-reinsurance accounting — gross→ceded→net rollup, recoverable leverage, and
the risk-transfer 10-10 / ERD test. Pure stdlib (no numpy/pandas).

Two modes:

  # cession (default): the financial-statement mechanics of ceding reinsurance.
  python reinsurance_accounting.py --gross-written 5000 --ceded-written 1000 \
      --gross-earned 4800 --ceded-earned 960 --gross-incurred 3200 \
      --ceded-incurred 700 --ceding-commission 250 --recoverables 1800 --surplus 6000

  # risk_transfer: does a treaty transfer enough risk to qualify for reinsurance
  # accounting (vs deposit accounting)? 10-10 rule + Expected Reinsurer Deficit.
  python reinsurance_accounting.py --mode risk_transfer --premium 100 --brokerage 5 \
      --scenarios "0.70:0,0.15:50,0.10:150,0.04:300,0.01:600"

  # From the warehouse (cession), --stdin, or --demo for the verified example:
  python reinsurance_accounting.py --db data/state.db --insurer TRV
  python reinsurance_accounting.py --demo
  python reinsurance_accounting.py --mode risk_transfer --demo

Monetary figures in USD millions (consistent units is all that matters). Outputs a
human table by default; --format json for a structured object.
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
    s = f"{x * 100:.1f}%"
    return s.replace("-0.0%", "0.0%")


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den in (None, 0):
        return None
    return num / den


# ── cession rollup ───────────────────────────────────────────────────────────

def cession(p: dict) -> dict:
    gw, cw = p.get("gross_written"), p.get("ceded_written")
    ge, ce = p.get("gross_earned"), p.get("ceded_earned")
    gi, ci = p.get("gross_incurred"), p.get("ceded_incurred")
    if gw is None and ge is None:
        sys.exit("reinsurance_accounting: need at least gross written or gross earned "
                 "premium (with the matching ceded figure).")
    net_written = (gw - cw) if (gw is not None and cw is not None) else None
    net_earned = (ge - ce) if (ge is not None and ce is not None) else None
    net_incurred = (gi - ci) if (gi is not None and ci is not None) else None
    out = {
        "gross_written": gw, "ceded_written": cw, "net_written": net_written,
        "gross_earned": ge, "ceded_earned": ce, "net_earned": net_earned,
        "gross_incurred": gi, "ceded_incurred": ci, "net_incurred": net_incurred,
        "cession_ratio_written": _ratio(cw, gw),
        "net_retention_written": _ratio(net_written, gw),
        "gross_loss_ratio": _ratio(gi, ge),
        "net_loss_ratio": _ratio(net_incurred, net_earned),
        "ceded_loss_ratio": _ratio(ci, ce),
        "ceding_commission": p.get("ceding_commission"),
        "recoverables": p.get("recoverables"),
        "surplus": p.get("surplus"),
        "recoverable_leverage": _ratio(p.get("recoverables"), p.get("surplus")),
        "warnings": [],
    }
    glr, clr = out["gross_loss_ratio"], out["ceded_loss_ratio"]
    if glr is not None and clr is not None:
        out["reinsurance_value"] = "favorable to cedant" if clr > glr else \
            "unfavorable to cedant" if clr < glr else "neutral"
    if out["recoverable_leverage"] is not None and out["recoverable_leverage"] > 1.0:
        out["warnings"].append(
            "recoverables exceed surplus (leverage > 100%) — reinsurer credit risk is "
            "material; check Schedule F authorized/collateral status.")
    return out


# ── risk-transfer test (10-10 rule + ERD) ────────────────────────────────────

def risk_transfer(p: dict) -> dict:
    prem = p.get("premium")
    if prem in (None, 0):
        sys.exit("reinsurance_accounting risk_transfer: --premium (>0) is required.")
    brokerage = p.get("brokerage", 0.0) or 0.0
    scen = p.get("scenarios")
    if not scen:
        sys.exit("reinsurance_accounting risk_transfer: --scenarios required "
                 "(\"prob:ceded_loss,…\" or JSON list of [prob, loss]).")
    tot_p = sum(s["prob"] for s in scen)
    if abs(tot_p - 1.0) > 1e-6:
        sys.exit(f"reinsurance_accounting: scenario probabilities sum to {tot_p:.4f}, "
                 "not 1.0.")
    rows, p_ge10, erd_num, mean_result = [], 0.0, 0.0, 0.0
    for s in scen:
        # reinsurer net result = premium − brokerage − ceded loss (PV ≈ nominal here)
        result = prem - brokerage - s["loss"]
        deficit = max(-result, 0.0)            # loss to the reinsurer
        deficit_pct = deficit / prem
        if deficit_pct >= 0.10:
            p_ge10 += s["prob"]
        erd_num += s["prob"] * deficit
        mean_result += s["prob"] * result
        rows.append({"prob": s["prob"], "ceded_loss": s["loss"], "reinsurer_result":
                     result, "deficit": deficit, "deficit_pct_of_premium": deficit_pct})
    erd = erd_num / prem
    ten_ten = p_ge10 >= 0.10
    out = {
        "premium": prem, "brokerage": brokerage, "scenarios": rows,
        "expected_reinsurer_result": mean_result,
        "prob_loss_ge_10pct": p_ge10,
        "ten_ten_pass": ten_ten,
        "erd": erd,
        "erd_pass": erd >= 0.01,
        "risk_transfer": ten_ten or erd >= 0.01,
        "accounting": ("reinsurance accounting (risk transferred)"
                       if (ten_ten or erd >= 0.01) else
                       "DEPOSIT accounting (insufficient risk transfer)"),
        "warnings": [],
    }
    if mean_result < 0:
        out["warnings"].append(
            "reinsurer is expected to LOSE money on this treaty — confirm the premium "
            "is complete (reinstatements, profit commission) before concluding.")
    return out


# ── inputs ───────────────────────────────────────────────────────────────────

def parse_scenarios(s: str) -> list[dict]:
    out = []
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            prob, loss = pair.split(":")
            out.append({"prob": float(prob), "loss": float(loss)})
        except ValueError:
            sys.exit(f"reinsurance_accounting: malformed scenario '{pair}' "
                     "(want prob:loss).")
    return out


def from_db(db_path: Path, insurer: str) -> dict:
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def fact(dataset: str, field: str) -> float | None:
            row = con.execute(
                "SELECT SUM(value) FROM insurer_xbrl_facts WHERE insurer=? AND "
                "dataset=? AND field=? AND accident_year IS NULL "
                "AND period_end=(SELECT MAX(period_end) FROM insurer_xbrl_facts "
                "WHERE insurer=? AND dataset=? AND field=?)",
                (insurer, dataset, field, insurer, dataset, field)).fetchone()
            return float(row[0]) if row and row[0] is not None else None

        cw = fact("reinsurance", "premiums_ceded")
        ce = fact("reinsurance", "ceded_premiums_earned")
        nw = fact("premiums", "premiums_written_net")
        ne = fact("premiums", "premiums_earned_net")
        rec = fact("reinsurance", "recoverable_unpaid")
        srow = con.execute(
            "SELECT value FROM statutory_facts WHERE insurer=? AND dataset='surplus' "
            "ORDER BY period DESC LIMIT 1", (insurer,)).fetchone()
        payload = {
            "ceded_written": cw, "ceded_earned": ce,
            "gross_written": (nw + cw) if (nw is not None and cw is not None) else None,
            "gross_earned": (ne + ce) if (ne is not None and ce is not None) else None,
            "recoverables": rec,
            "surplus": float(srow[0]) if srow else None,
        }
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO_CESSION = {
    "gross_written": 5000, "ceded_written": 1000, "gross_earned": 4800,
    "ceded_earned": 960, "gross_incurred": 3200, "ceded_incurred": 700,
    "ceding_commission": 250, "recoverables": 1800, "surplus": 6000,
}
_DEMO_RT = {"premium": 100, "brokerage": 5,
            "scenarios": [{"prob": 0.70, "loss": 0}, {"prob": 0.15, "loss": 50},
                          {"prob": 0.10, "loss": 150}, {"prob": 0.04, "loss": 300},
                          {"prob": 0.01, "loss": 600}]}


def render_cession(o: dict) -> str:
    L = ["CEDED REINSURANCE — CESSION ROLLUP ($M)"]
    def line(lbl, g, c, n):
        f = lambda v: f"{v:,.1f}" if v is not None else "—"
        L.append(f"  {lbl:<20} gross {f(g):>10}   ceded {f(c):>9}   net {f(n):>10}")
    line("Written premium", o["gross_written"], o["ceded_written"], o["net_written"])
    line("Earned premium", o["gross_earned"], o["ceded_earned"], o["net_earned"])
    line("Incurred loss", o["gross_incurred"], o["ceded_incurred"], o["net_incurred"])
    L.append(f"  cession ratio (written) {_pct(o['cession_ratio_written'])}   "
             f"net retention {_pct(o['net_retention_written'])}")
    L.append(f"  loss ratio  gross {_pct(o['gross_loss_ratio'])}   "
             f"net {_pct(o['net_loss_ratio'])}   ceded {_pct(o['ceded_loss_ratio'])}"
             f"   ({o.get('reinsurance_value','—')})")
    L.append(f"  recoverables {o['recoverables']:,.1f}   surplus {o['surplus']:,.1f}   "
             f"recoverable leverage {_pct(o['recoverable_leverage'])}"
             if o.get("recoverables") is not None and o.get("surplus") is not None
             else "  recoverable leverage —")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def render_rt(o: dict) -> str:
    L = ["REINSURANCE RISK-TRANSFER TEST (10-10 / ERD)",
         f"  premium {o['premium']:,.1f}   brokerage {o['brokerage']:,.1f}"]
    for s in o["scenarios"]:
        L.append(f"  p={s['prob']:.2f}  ceded loss {s['ceded_loss']:>7,.1f}  "
                 f"reinsurer result {s['reinsurer_result']:>8,.1f}  "
                 f"deficit {_pct(s['deficit_pct_of_premium'])}")
    L.append(f"  E[reinsurer result] {o['expected_reinsurer_result']:+,.1f}")
    L.append(f"  P(loss ≥ 10% of premium) {_pct(o['prob_loss_ge_10pct'])}  "
             f"→ 10-10 {'PASS' if o['ten_ten_pass'] else 'FAIL'}")
    L.append(f"  ERD {_pct(o['erd'])}  → {'PASS' if o['erd_pass'] else 'FAIL'} (≥1%)")
    L.append(f"  ⇒ {o['accounting']}")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Ceded reinsurance accounting + risk transfer.")
    ap.add_argument("--mode", choices=["cession", "risk_transfer"], default="cession")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    for d in ("gross_written", "ceded_written", "gross_earned", "ceded_earned",
              "gross_incurred", "ceded_incurred", "ceding_commission", "recoverables",
              "surplus", "premium", "brokerage"):
        ap.add_argument(f"--{d.replace('_', '-')}", type=float)
    ap.add_argument("--scenarios", help='"prob:loss,prob:loss,…"')
    args = ap.parse_args()

    if args.demo:
        p = dict(_DEMO_RT if args.mode == "risk_transfer" else _DEMO_CESSION)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"reinsurance_accounting: bad JSON on stdin ({e}).")
        if p.get("mode"):
            args.mode = p["mode"]
        if isinstance(p.get("scenarios"), list) and p["scenarios"] \
                and isinstance(p["scenarios"][0], (list, tuple)):
            p["scenarios"] = [{"prob": a, "loss": b} for a, b in p["scenarios"]]
    elif args.db:
        if not args.insurer:
            sys.exit("reinsurance_accounting: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in ("mode", "db", "insurer", "stdin", "demo",
                                           "format", "scenarios"):
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args) if getattr(args, d) is not None
             and d not in ("mode", "db", "insurer", "stdin", "demo", "format", "scenarios")}
        if args.scenarios:
            p["scenarios"] = parse_scenarios(args.scenarios)
        if not p:
            sys.exit("reinsurance_accounting: no inputs. Pass flags, --stdin, --db, or --demo.")
    if args.scenarios and "scenarios" not in p:
        p["scenarios"] = parse_scenarios(args.scenarios)

    out = risk_transfer(p) if args.mode == "risk_transfer" else cession(p)
    if args.insurer:
        out["insurer"] = args.insurer
    if args.format == "json":
        print(json.dumps(out, indent=2))
    else:
        print(render_rt(out) if args.mode == "risk_transfer" else render_cession(out))


if __name__ == "__main__":
    main()
