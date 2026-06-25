#!/usr/bin/env python3
"""GAAP shareholders' equity ↔ NAIC statutory surplus — reconciliation + the
change-in-surplus decomposition. Pure stdlib (no numpy/pandas) so it runs anywhere.

Two modes:

  # Bridge: GAAP equity → implied statutory surplus via the reconciling items.
  python statutory_gaap_bridge.py --gaap-equity 25000 --aoci -1200 \
      --dac 3000 --goodwill-intangibles 1500 --non-admitted-dta 400 \
      --provision-reinsurance 100 --reported-surplus 21000

  # Change-in-surplus: decompose the YoY move in statutory surplus.
  python statutory_gaap_bridge.py --change --begin-surplus 20000 \
      --stat-net-income 2400 --unrealized-change 300 --dividends 1200 ...

  # From the warehouse (read-only) — pulls GAAP equity / DAC / AOCI / reported
  # surplus where ingested; the rest you supply from the annual statement:
  python statutory_gaap_bridge.py --db data/state.db --insurer TRV

  # Or pipe a JSON payload, and --demo for the verified worked example:
  echo '{"gaap_equity":25000,"aoci":-1200,"dac":3000}' | python … --stdin
  python statutory_gaap_bridge.py --demo

All figures in USD millions. Sign conventions are documented inline and in
reference.md. Outputs a human waterfall by default; --format json for a structured
object.

WHY this matters: a P&C insurer reports GAAP equity (10-K) AND statutory surplus
(NAIC annual statement). They are NOT the same number — STAT is the regulator's
liquidation-biased capital and is what RBC, dividend capacity and leverage ratios
divide by. The gap is mostly DAC (GAAP asset / STAT expensed), bonds (GAAP AFS fair
value with AOCI / STAT amortized cost), and other non-admitted assets.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Reconciling items, GAAP common equity → statutory surplus. Each entry is
# (cli_dest, label, sign) where surplus = gaap_equity + Σ sign·value.
#   AOCI: STAT carries AFS bonds at amortized cost, so it EXCLUDES the AFS
#         unrealized gain GAAP parks in AOCI → subtract AOCI (a negative AOCI,
#         i.e. an unrealized LOSS, therefore ADDS back to surplus).
#   The non-admitted assets (DAC, goodwill/intangibles, non-admitted DTA) are
#         carried as assets under GAAP but disallowed by SAP → subtract.
#   Provision for reinsurance is a STAT-only charge to surplus → subtract.
_BRIDGE_ITEMS = [
    ("aoci", "AOCI — reverse AFS bonds to amortized cost", -1),
    ("dac", "DAC — non-admitted (STAT expenses acquisition cost)", -1),
    ("goodwill_intangibles", "Goodwill & intangibles — non-admitted", -1),
    ("non_admitted_dta", "Net non-admitted deferred tax asset", -1),
    ("provision_reinsurance", "Provision for reinsurance (SAP charge)", -1),
    ("other_non_admitted", "Other non-admitted / write-ins", -1),
]

# Change-in-surplus drivers (the "capital and surplus account"). surplus_end =
# begin + Σ value (dividends/non-admitted increases enter as negatives).
_CHANGE_ITEMS = [
    ("stat_net_income", "Statutory net income"),
    ("unrealized_change", "Change in net unrealized capital gains"),
    ("dividends", "Stockholder dividends (−)"),
    ("paid_in", "Capital & paid-in surplus contributed"),
    ("deferred_tax_change", "Change in net deferred income tax"),
    ("non_admitted_change", "Change in non-admitted assets"),
    ("provision_change", "Change in provision for reinsurance"),
    ("other_change", "Other / aggregate write-ins"),
]


def _pct(x: float) -> str:
    s = f"{x * 100:+.1f}%"
    return s.replace("-0.0%", "0.0%")


def _musd(x: float) -> str:
    s = f"{x:+,.1f}"
    return "0.0" if s in ("+0.0", "-0.0") else s


def from_db(db_path: Path, insurer: str) -> dict:
    """Pull what the warehouse ingests; leave the rest for the user to supply."""
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def latest(dataset: str, field: str) -> float | None:
            row = con.execute(
                "SELECT value FROM insurer_xbrl_facts WHERE insurer=? AND dataset=? "
                "AND field=? AND segment IS NULL AND product IS NULL "
                "ORDER BY period_end DESC, as_of DESC LIMIT 1",
                (insurer, dataset, field)).fetchone()
            return float(row[0]) if row else None

        surplus_row = con.execute(
            "SELECT value FROM statutory_facts WHERE insurer=? AND dataset='surplus' "
            "ORDER BY period DESC LIMIT 1", (insurer,)).fetchone()
        payload = {
            "insurer": insurer,
            "gaap_equity": latest("equity", "common_equity"),
            "aoci": latest("aoci", "oci_net"),
            "dac": latest("dac", "dac_balance"),
            "reported_surplus": float(surplus_row[0]) if surplus_row else None,
        }
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


def bridge(p: dict) -> dict:
    if p.get("gaap_equity") is None:
        sys.exit("statutory_gaap_bridge: --gaap-equity is required (or supply it via "
                 "--db / --stdin). It anchors the reconciliation.")
    gaap = float(p["gaap_equity"])
    steps, missing = [], []
    running = gaap
    for dest, label, sign in _BRIDGE_ITEMS:
        if p.get(dest) is None:
            missing.append(dest)
            continue
        delta = sign * float(p[dest])
        running += delta
        steps.append({"item": label, "input": float(p[dest]), "effect": delta,
                      "running": running})
    implied = running
    out = {"gaap_equity": gaap, "steps": steps, "implied_surplus": implied,
           "missing_inputs": missing, "warnings": []}
    if missing:
        out["warnings"].append(
            "treated as 0 (not in warehouse — supply from the annual statement): "
            + ", ".join(missing))
    if p.get("reported_surplus") is not None:
        rep = float(p["reported_surplus"])
        out["reported_surplus"] = rep
        out["unexplained_residual"] = rep - implied
        if abs(rep - implied) > 0.02 * max(abs(rep), 1.0):
            out["warnings"].append(
                f"residual {_musd(rep - implied)}M (> 2% of surplus) — reconciling "
                "items are incomplete or estimated.")
    return out


def change(p: dict) -> dict:
    if p.get("begin_surplus") is None:
        sys.exit("statutory_gaap_bridge --change: --begin-surplus is required.")
    begin = float(p["begin_surplus"])
    drivers, running = [], begin
    for dest, label in _CHANGE_ITEMS:
        if p.get(dest) is None:
            continue
        val = float(p[dest])
        running += val
        drivers.append({"driver": label, "value": val})
    total = running - begin
    for d in drivers:
        d["share_of_change"] = (d["value"] / total) if total else 0.0
    out = {"begin_surplus": begin, "drivers": drivers, "total_change": total,
           "end_surplus": running, "warnings": []}
    if p.get("reported_end_surplus") is not None:
        rep = float(p["reported_end_surplus"])
        out["reported_end_surplus"] = rep
        out["unexplained_residual"] = rep - running
    return out


# ── demo (verified worked example — asserted in tests/test_skill_scripts.py) ──
_DEMO_BRIDGE = {
    "gaap_equity": 25000, "aoci": -1200, "dac": 3000, "goodwill_intangibles": 1500,
    "non_admitted_dta": 400, "provision_reinsurance": 100, "other_non_admitted": 0,
    "reported_surplus": 21200,
}
_DEMO_CHANGE = {
    "begin_surplus": 20000, "stat_net_income": 2400, "unrealized_change": 300,
    "dividends": -1200, "paid_in": 0, "deferred_tax_change": 150,
    "non_admitted_change": -250, "provision_change": -50, "other_change": -150,
}


def render_text(out: dict, mode: str) -> str:
    L = []
    if mode == "bridge":
        L.append(f"GAAP → STATUTORY SURPLUS BRIDGE  ({out.get('insurer','—')})")
        L.append(f"  GAAP shareholders' equity {'':>26} {out['gaap_equity']:>12,.1f}")
        for s in out["steps"]:
            L.append(f"  {s['item']:<48} {_musd(s['effect']):>12}  → {s['running']:,.1f}")
        L.append(f"  {'= implied statutory surplus':<48} {'':>12}    {out['implied_surplus']:,.1f}")
        if "reported_surplus" in out:
            L.append(f"  {'reported statutory surplus':<48} {'':>12}    {out['reported_surplus']:,.1f}")
            L.append(f"  {'unexplained residual':<48} {_musd(out['unexplained_residual']):>12}")
    else:
        L.append("STATUTORY SURPLUS — CHANGE DECOMPOSITION")
        L.append(f"  Beginning surplus {'':>34} {out['begin_surplus']:>12,.1f}")
        for d in out["drivers"]:
            L.append(f"  {d['driver']:<48} {_musd(d['value']):>12}  ({_pct(d['share_of_change'])} of Δ)")
        L.append(f"  {'= total change':<48} {_musd(out['total_change']):>12}")
        L.append(f"  {'= ending surplus':<48} {'':>12}    {out['end_surplus']:,.1f}")
    for w in out.get("warnings", []):
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="GAAP equity ↔ statutory surplus bridge.")
    ap.add_argument("--change", action="store_true", help="change-in-surplus mode")
    ap.add_argument("--db")
    ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    # bridge inputs
    for dest in ("gaap_equity", "aoci", "dac", "goodwill_intangibles",
                 "non_admitted_dta", "provision_reinsurance", "other_non_admitted",
                 "reported_surplus"):
        ap.add_argument(f"--{dest.replace('_', '-')}", type=float)
    # change inputs
    for dest in ("begin_surplus", "stat_net_income", "unrealized_change", "dividends",
                 "paid_in", "deferred_tax_change", "non_admitted_change",
                 "provision_change", "other_change", "reported_end_surplus"):
        ap.add_argument(f"--{dest.replace('_', '-')}", type=float)
    args = ap.parse_args()

    if args.demo:
        p = dict(_DEMO_CHANGE if args.change else _DEMO_BRIDGE)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"statutory_gaap_bridge: bad JSON on stdin ({e}).")
        if p.get("change"):
            args.change = True
    elif args.db:
        if not args.insurer:
            sys.exit("statutory_gaap_bridge: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        # let CLI flags override / fill warehouse gaps
        for dest in vars(args):
            if getattr(args, dest) is not None and dest not in (
                    "change", "db", "insurer", "stdin", "demo", "format"):
                p[dest] = getattr(args, dest)
    else:
        p = {d: getattr(args, d) for d in vars(args)
             if getattr(args, d) is not None and d not in (
                 "change", "db", "insurer", "stdin", "demo", "format")}
        if not p:
            sys.exit("statutory_gaap_bridge: no inputs. Pass flags, --stdin, --db, or --demo.")

    out = change(p) if args.change else bridge(p)
    if args.insurer:
        out["insurer"] = args.insurer
    mode = "change" if args.change else "bridge"
    if args.format == "json":
        print(json.dumps(out, indent=2))
    else:
        print(render_text(out, mode))


if __name__ == "__main__":
    main()
