#!/usr/bin/env python3
"""P&C insurer capital adequacy — NAIC RBC (R0–R5 + covariance, action levels),
AM Best BCAR, and statutory leverage / IRIS ratios. Pure stdlib (no numpy/pandas).

Computes whichever blocks the inputs support, in one pass:
  • RBC   — needs R0..R5 components (+ TAC; defaults to surplus)
  • leverage/IRIS — needs net written premium + net reserves + surplus
  • BCAR  — needs available capital + net required capital (at a VaR level)

  python capital_adequacy.py --r0 50 --r1 600 --r2 500 --r3 700 --r4 2200 --r5 1500 \
      --tac 6000 --nwp 4000 --reserves 10000 --surplus 6000 \
      --available-capital 6500 --net-required-capital 4500 --bcar-var 99.6

  # From the warehouse (leverage block; RBC/BCAR components you supply):
  python capital_adequacy.py --db data/state.db --insurer TRV --r4 2200 ...

  # --stdin for a JSON payload, --demo for the verified worked example.

All $ figures in USD millions. RBC pages and BCAR inputs are NOT in the warehouse —
supply them from the statutory blank / a rating report; leverage ratios are computed
directly from ingested facts. Outputs a human report; --format json for structured.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

# NAIC action levels, as a multiple of ACL (Authorized Control Level).
# 200% of ACL == the after-covariance RBC itself (ACL = ½·RBC).
_RBC_BANDS = [  # (lower bound on TAC/ACL, label)
    (2.00, "No action — adequately capitalized (≥200% ACL)"),
    (1.50, "Company Action Level (150–200% ACL) — file a plan"),
    (1.00, "Regulatory Action Level (100–150% ACL) — regulator examines/orders"),
    (0.70, "Authorized Control Level (70–100% ACL) — regulator may seize"),
    (0.00, "Mandatory Control Level (<70% ACL) — regulator must seize"),
]
_BCAR_BANDS = [  # (lower bound on BCAR at the quoted VaR, assessment)
    (0.25, "Strongest"),
    (0.10, "Very Strong"),
    (0.00, "Strong/Adequate"),
    (-1e18, "Weak — capital below required"),
]
# IRIS usual-range upper bounds (P&C), as ratios.
_IRIS = {
    "gross_premium_to_surplus": (9.00, "IRIS 1: gross written premium / surplus"),
    "net_premium_to_surplus": (3.00, "IRIS 2: net written premium / surplus"),
    "reserves_to_surplus": (None, "net loss & LAE reserves / surplus"),
    "net_leverage": (None, "(NWP + reserves) / surplus"),
}


def _band(value: float, bands) -> str:
    for lo, label in bands:
        if value >= lo:
            return label
    return bands[-1][1]


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%".replace("-0.0%", "0.0%")


def rbc(p: dict) -> dict | None:
    rs = [p.get(f"r{i}") for i in range(6)]
    if any(r is None for r in rs):
        return None
    r0, r1, r2, r3, r4, r5 = (float(r) for r in rs)
    after_cov = r0 + math.sqrt(r1 ** 2 + r2 ** 2 + r3 ** 2 + r4 ** 2 + r5 ** 2)
    acl = 0.5 * after_cov
    tac = float(p.get("tac") if p.get("tac") is not None else p.get("surplus"))
    if tac is None or acl == 0:
        return None
    ratio = tac / acl
    return {"r0": r0, "r1": r1, "r2": r2, "r3": r3, "r4": r4, "r5": r5,
            "rbc_after_covariance": after_cov, "acl": acl, "tac": tac,
            "rbc_ratio_pct_of_acl": ratio, "action_level": _band(ratio, _RBC_BANDS),
            "covariance_benefit": (r0 + r1 + r2 + r3 + r4 + r5) - after_cov}


def leverage(p: dict) -> dict | None:
    surplus = p.get("surplus")
    if not surplus:
        return None
    nwp, gwp, reserves = p.get("nwp"), p.get("gwp"), p.get("reserves")
    out = {"surplus": float(surplus), "flags": []}
    if gwp is not None:
        out["gross_premium_to_surplus"] = gwp / surplus
    if nwp is not None:
        out["net_premium_to_surplus"] = nwp / surplus
    if reserves is not None:
        out["reserves_to_surplus"] = reserves / surplus
    if nwp is not None and reserves is not None:
        out["net_leverage"] = (nwp + reserves) / surplus
    for key, (upper, _label) in _IRIS.items():
        if upper is not None and key in out and out[key] > upper:
            out["flags"].append(f"{key} {out[key]:.2f}× exceeds IRIS usual range "
                                f"(<{upper:.2f}×) — aggressive leverage.")
    return out


def bcar(p: dict) -> dict | None:
    ac, nrc = p.get("available_capital"), p.get("net_required_capital")
    if ac is None or nrc is None or ac == 0:
        return None
    score = (ac - nrc) / ac
    return {"available_capital": float(ac), "net_required_capital": float(nrc),
            "var_level": p.get("bcar_var", 99.6), "bcar": score,
            "assessment": _band(score, _BCAR_BANDS)}


def assess(p: dict) -> dict:
    out = {"rbc": rbc(p), "leverage": leverage(p), "bcar": bcar(p), "warnings": []}
    if not any(out[k] for k in ("rbc", "leverage", "bcar")):
        sys.exit("capital_adequacy: not enough inputs for any block. Provide R0..R5 "
                 "(RBC), or premium+reserves+surplus (leverage), or available + "
                 "required capital (BCAR).")
    if out["rbc"] is None and out["leverage"]:
        out["warnings"].append("no RBC components supplied — leverage ratios shown are "
                               "a coarse solvency proxy, not the regulatory RBC ratio.")
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

        srow = con.execute(
            "SELECT value FROM statutory_facts WHERE insurer=? AND dataset='surplus' "
            "ORDER BY period DESC LIMIT 1", (insurer,)).fetchone()
        payload = {
            "nwp": fact("premiums", "premiums_written_net"),
            "reserves": fact("unpaid_claims", "liability_net"),
            "surplus": float(srow[0]) if srow else fact("equity", "common_equity"),
        }
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO = {"r0": 50, "r1": 600, "r2": 500, "r3": 700, "r4": 2200, "r5": 1500,
         "tac": 6000, "nwp": 4000, "gwp": 5000, "reserves": 10000, "surplus": 6000,
         "available_capital": 6500, "net_required_capital": 4500, "bcar_var": 99.6}


def render(o: dict) -> str:
    L = ["CAPITAL ADEQUACY"]
    r = o["rbc"]
    if r:
        L.append("  ── NAIC RBC ──")
        L.append(f"    R0 {r['r0']:,.0f}  R1 {r['r1']:,.0f}  R2 {r['r2']:,.0f}  "
                 f"R3 {r['r3']:,.0f}  R4 {r['r4']:,.0f}  R5 {r['r5']:,.0f}")
        L.append(f"    RBC after covariance {r['rbc_after_covariance']:,.1f}  "
                 f"(diversification benefit {r['covariance_benefit']:,.1f})")
        L.append(f"    ACL {r['acl']:,.1f}   TAC {r['tac']:,.1f}   "
                 f"RBC ratio {_pct(r['rbc_ratio_pct_of_acl'])} of ACL")
        L.append(f"    → {r['action_level']}")
    lev = o["leverage"]
    if lev:
        L.append("  ── Leverage / IRIS ──")
        for key, (_u, label) in _IRIS.items():
            if key in lev:
                L.append(f"    {label:<42} {lev[key]:.2f}×")
        for f in lev["flags"]:
            L.append(f"    ⚠ {f}")
    b = o["bcar"]
    if b:
        L.append("  ── AM Best BCAR ──")
        L.append(f"    available {b['available_capital']:,.1f}  required "
                 f"{b['net_required_capital']:,.1f}  @VaR {b['var_level']}")
        L.append(f"    BCAR {_pct(b['bcar'])} → {b['assessment']}")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="P&C capital adequacy — RBC / BCAR / leverage.")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    for d in ("r0", "r1", "r2", "r3", "r4", "r5", "tac", "nwp", "gwp", "reserves",
              "surplus", "available_capital", "net_required_capital", "bcar_var"):
        ap.add_argument(f"--{d.replace('_', '-')}", type=float)
    args = ap.parse_args()

    if args.demo:
        p = dict(_DEMO)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"capital_adequacy: bad JSON on stdin ({e}).")
    elif args.db:
        if not args.insurer:
            sys.exit("capital_adequacy: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in ("db", "insurer", "stdin", "demo", "format"):
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args) if getattr(args, d) is not None
             and d not in ("db", "insurer", "stdin", "demo", "format")}
        if not p:
            sys.exit("capital_adequacy: no inputs. Pass flags, --stdin, --db, or --demo.")

    out = assess(p)
    if args.insurer:
        out["insurer"] = args.insurer
    print(json.dumps(out, indent=2) if args.format == "json" else render(out))


if __name__ == "__main__":
    main()
