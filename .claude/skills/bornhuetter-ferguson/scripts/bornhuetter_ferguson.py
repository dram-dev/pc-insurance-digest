#!/usr/bin/env python3
"""Bornhuetter-Ferguson (and Cape Cod) reserving — blend chain-ladder development
with an a-priori expected loss so a thin, green accident-year diagonal doesn't get
multiplied by a large CDF into a wild ultimate. Pure stdlib (no numpy/pandas).

The development math is the SAME volume-weighted chain-ladder as
src/digest/reserving.py and the reserving-chain-ladder skill, so the CDFs reconcile;
this script then applies:

  IBNR_BF      = Apriori_ultimate × (1 − 1/CDF)        # a-priori for the UNreported part
  Ultimate_BF  = Latest_actual + IBNR_BF                # actuals for the reported part
  Apriori      = premium × ELR        (or a directly supplied a-priori ultimate)

Two ways to get the ELR:
  • supply it:  --elr 0.72          (a-priori / plan / budget loss ratio)
  • derive it:  --cape-cod          (Stanard-Bühlmann: ELR = Σ actual / Σ used-up
                                      premium, where used-up = premium × 1/CDF)

Compare every AY to pure chain-ladder (Latest × CDF) so you can see exactly where —
and how much — BF pulls a green year back toward the a-priori.

Input modes (same shape as chain_ladder.py, plus premiums/ELR):

  # From the warehouse (read-only), latest snapshot unless --as-of:
  python bornhuetter_ferguson.py --db data/state.db \
      --insurer PGR --lob personal_auto --metric incurred \
      --premiums "2021:30000,2022:33000,2023:35000,2024:37000" --elr 0.72 --cape-cod

  # Ad-hoc triangle + premiums + ELR on stdin:
  echo '{"cells":[{"ay":2019,"dev":0,"value":1000}, ...],
         "premiums":{"2019":2400,"2020":2640}, "elr":0.75}' \
      | python bornhuetter_ferguson.py --stdin

  # Self-checking worked example (reconciles to reference.md):
  python bornhuetter_ferguson.py --demo
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path


# ── Input ──────────────────────────────────────────────────────────────────


def cells_from_db(db_path: Path, insurer: str, lob: str, metric: str,
                  as_of: str | None) -> tuple[list[dict], str]:
    """Read triangle cells from loss_triangles (read-only). Latest as_of if None."""
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if as_of is None:
            row = conn.execute(
                "SELECT MAX(as_of) AS a FROM loss_triangles "
                "WHERE insurer=? AND lob=? AND metric=?",
                (insurer, lob, metric),
            ).fetchone()
            as_of = row["a"] if row else None
        if as_of is None:
            sys.exit(f"No triangle for insurer={insurer} lob={lob} metric={metric}.")
        rows = conn.execute(
            "SELECT accident_year AS ay, dev_period AS dev, cumulative_value AS value "
            "FROM loss_triangles WHERE insurer=? AND lob=? AND metric=? AND as_of=? "
            "ORDER BY accident_year, dev_period",
            (insurer, lob, metric, as_of),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows], as_of


def cells_from_stdin() -> tuple[list[dict], dict[int, float], float | None]:
    """Accept {"cells":[{ay,dev,value}], "premiums":{ay:val}, "elr":x} (or a dense
    {"accident_years","dev_periods","rows"} matrix). Returns (cells, premiums, elr)."""
    payload = json.load(sys.stdin)
    if "cells" in payload:
        cells = [{"ay": c["ay"], "dev": c["dev"], "value": c["value"]}
                 for c in payload["cells"] if c.get("value") is not None]
    else:
        ays, devs = payload["accident_years"], payload["dev_periods"]
        cells = []
        for i, ay in enumerate(ays):
            for j, dev in enumerate(devs):
                v = payload["rows"][i][j]
                if v is not None:
                    cells.append({"ay": ay, "dev": dev, "value": float(v)})
    premiums = {int(k): float(v) for k, v in payload.get("premiums", {}).items()}
    elr = float(payload["elr"]) if payload.get("elr") is not None else None
    return cells, premiums, elr


def parse_premiums(spec: str | None) -> dict[int, float]:
    """'2019:2400,2020:2640' → {2019: 2400.0, 2020: 2640.0}."""
    if not spec:
        return {}
    out: dict[int, float] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        ay, val = pair.split(":")
        out[int(ay)] = float(val)
    return out


# ── Development (volume-weighted chain-ladder, matching reserving.py) ─────────


def develop(cells: list[dict], tail: float = 1.0, min_credible: int = 3) -> dict:
    """Volume-weighted age-to-age factors → CDF per dev → per-AY latest/CDF/%unreported."""
    if not cells:
        sys.exit("Empty triangle.")
    ays = sorted({c["ay"] for c in cells})
    devs = sorted({c["dev"] for c in cells})
    grid = {(c["ay"], c["dev"]): float(c["value"]) for c in cells}
    n_dev = len(devs)
    if n_dev < 2:
        sys.exit("Need at least two development periods to compute a factor.")

    warnings: list[str] = []
    factors = []
    for j in range(n_dev - 1):
        d0, d1 = devs[j], devs[j + 1]
        num = den = 0.0
        ratios = []
        for ay in ays:
            a, b = grid.get((ay, d0)), grid.get((ay, d1))
            if a is not None and b is not None and a != 0:
                num += b
                den += a
                ratios.append(b / a)
        vw = (num / den) if den > 0 else 1.0
        n = len(ratios)
        cv = (statistics.stdev(ratios) / statistics.fmean(ratios)
              if n >= 2 and statistics.fmean(ratios) else None)
        if n < min_credible:
            warnings.append(f"factor dev {d0}->{d1} rests on {n} link ratio(s) "
                            f"(< {min_credible}) — low credibility; this is exactly "
                            f"where BF helps most.")
        factors.append({"from_dev": d0, "to_dev": d1, "factor": round(vw, 6),
                        "cv": round(cv, 4) if cv is not None else None, "n_ratios": n})

    cdf = [1.0] * n_dev
    cdf[n_dev - 1] = tail
    for j in range(n_dev - 2, -1, -1):
        cdf[j] = cdf[j + 1] * factors[j]["factor"]

    per_ay = []
    for ay in ays:
        observed = [j for j, d in enumerate(devs) if (ay, d) in grid]
        if not observed:
            continue
        last_j = observed[-1]
        latest = grid[(ay, devs[last_j])]
        f = cdf[last_j]
        per_ay.append({
            "accident_year": ay, "latest_dev": devs[last_j], "latest": latest,
            "cdf": f, "pct_developed": 1.0 / f if f else None,
            "pct_unreported": (1.0 - 1.0 / f) if f else None,
        })

    return {"accident_years": ays, "dev_periods": devs, "tail": tail,
            "factors": factors, "cdf": {devs[j]: round(cdf[j], 6) for j in range(n_dev)},
            "per_ay": per_ay, "warnings": warnings}


# ── Cape Cod ELR + BF application ─────────────────────────────────────────────


def cape_cod_elr(per_ay: list[dict], premiums: dict[int, float]) -> dict | None:
    """Stanard-Bühlmann ELR = Σ latest actual / Σ (premium × pct_developed).
    Needs a premium for every developed AY; returns None otherwise."""
    loss_sum = used_up = 0.0
    for r in per_ay:
        prem = premiums.get(r["accident_year"])
        if prem is None or r["pct_developed"] is None:
            return None
        loss_sum += r["latest"]
        used_up += prem * r["pct_developed"]
    if used_up <= 0:
        return None
    return {"elr": loss_sum / used_up, "actual_total": round(loss_sum, 2),
            "used_up_premium": round(used_up, 2)}


def apply_bf(per_ay: list[dict], premiums: dict[int, float], elr: float,
             apriori_override: dict[int, float] | None = None) -> list[dict]:
    """Per AY: a-priori ultimate (premium×ELR, or override), CL ultimate (latest×CDF),
    BF ultimate (latest + apriori×%unreported) and the BF IBNR."""
    rows = []
    for r in per_ay:
        ay = r["accident_year"]
        cl_ult = r["latest"] * r["cdf"]
        apriori = None
        if apriori_override and ay in apriori_override:
            apriori = apriori_override[ay]
        elif ay in premiums:
            apriori = premiums[ay] * elr
        if apriori is None:
            rows.append({**r, "apriori_ult": None, "cl_ult": round(cl_ult, 2),
                         "cl_ibnr": round(cl_ult - r["latest"], 2),
                         "bf_ult": None, "bf_ibnr": None,
                         "note": "no premium/a-priori → CL only"})
            continue
        bf_ibnr = apriori * r["pct_unreported"]
        bf_ult = r["latest"] + bf_ibnr
        rows.append({**r, "apriori_ult": round(apriori, 2),
                     "cl_ult": round(cl_ult, 2), "cl_ibnr": round(cl_ult - r["latest"], 2),
                     "bf_ult": round(bf_ult, 2), "bf_ibnr": round(bf_ibnr, 2)})
    return rows


def totals(rows: list[dict]) -> dict:
    f = lambda key: round(sum(r[key] for r in rows if r.get(key) is not None), 2)
    return {"latest": f("latest"), "cl_ult": f("cl_ult"), "cl_ibnr": f("cl_ibnr"),
            "bf_ult": f("bf_ult"), "bf_ibnr": f("bf_ibnr")}


# ── Rendering ────────────────────────────────────────────────────────────────


def render_text(dev: dict, rows: list[dict], elr: float, elr_source: str,
                cc: dict | None, label: str) -> str:
    out = [f"Bornhuetter-Ferguson estimate — {label}", "=" * 66, ""]
    out.append("Age-to-age factors (volume-weighted) → CDF:")
    for fct in dev["factors"]:
        cv = f"CV={fct['cv']:.3f}" if fct["cv"] is not None else "CV=n/a"
        out.append(f"  dev {fct['from_dev']}->{fct['to_dev']}: "
                   f"{fct['factor']:.4f}  ({cv}, n={fct['n_ratios']})")
    out.append("  CDF: " + ", ".join(f"{d}:{v}" for d, v in dev["cdf"].items()))
    out += ["", f"Expected loss ratio (ELR): {elr:.4f}  [{elr_source}]"]
    if cc:
        out.append(f"  Cape Cod (Stanard-Bühlmann): ELR = {cc['actual_total']:,.0f}"
                   f" / {cc['used_up_premium']:,.0f} used-up premium = {cc['elr']:.4f}")
    out += ["", "Per accident-year (BF vs pure chain-ladder):",
            f"  {'AY':>5} {'latest':>12} {'%unrep':>8} {'apriori':>12} "
            f"{'CL ult':>12} {'BF ult':>12} {'BF IBNR':>12}"]
    for r in rows:
        unrep = f"{r['pct_unreported']*100:.1f}%" if r["pct_unreported"] is not None else "  n/a"
        ap = f"{r['apriori_ult']:>12,.0f}" if r["apriori_ult"] is not None else f"{'n/a':>12}"
        bfu = f"{r['bf_ult']:>12,.0f}" if r["bf_ult"] is not None else f"{'n/a':>12}"
        bfi = f"{r['bf_ibnr']:>12,.0f}" if r["bf_ibnr"] is not None else f"{'n/a':>12}"
        out.append(f"  {r['accident_year']:>5} {r['latest']:>12,.0f} {unrep:>8} "
                   f"{ap} {r['cl_ult']:>12,.0f} {bfu} {bfi}")
    t = totals(rows)
    out += ["", "Totals:",
            f"  latest (current diagonal): {t['latest']:>16,.0f}",
            f"  chain-ladder ultimate:     {t['cl_ult']:>16,.0f}   IBNR {t['cl_ibnr']:>14,.0f}",
            f"  Bornhuetter-Ferguson ult:  {t['bf_ult']:>16,.0f}   IBNR {t['bf_ibnr']:>14,.0f}"]
    if t["cl_ibnr"]:
        diff = t["bf_ibnr"] - t["cl_ibnr"]
        out.append(f"  BF − CL IBNR:              {diff:>16,.0f}   "
                   f"({diff / t['cl_ibnr'] * 100:+.1f}% vs CL)")
    if dev["warnings"]:
        out += ["", "⚠ Credibility / data warnings:"]
        out += [f"  - {w}" for w in dev["warnings"]]
    out += ["", "Read: BF trusts ACTUALS for the developed part and the A-PRIORI for the",
            "undeveloped part, so it diverges from CL most on the youngest AYs (high",
            "%unreported × large CDF). Mature AYs (low %unreported) → BF ≈ CL. IBNR is",
            "pure-IBNR on an INCURRED triangle, total-unpaid on a PAID one — don't mix."]
    return "\n".join(out)


# ── Demo (self-check; reconciles to reference.md worked example) ──────────────

_DEMO = {
    "cells": [
        {"ay": 2019, "dev": 0, "value": 1000}, {"ay": 2019, "dev": 1, "value": 1500},
        {"ay": 2019, "dev": 2, "value": 1750}, {"ay": 2019, "dev": 3, "value": 1800},
        {"ay": 2020, "dev": 0, "value": 1200}, {"ay": 2020, "dev": 1, "value": 1800},
        {"ay": 2020, "dev": 2, "value": 2100},
        {"ay": 2021, "dev": 0, "value": 1100}, {"ay": 2021, "dev": 1, "value": 1650},
        {"ay": 2022, "dev": 0, "value": 1300},
    ],
    "premiums": {2019: 2400, 2020: 2640, 2021: 2200, 2022: 2600},
    "elr": 0.75,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Bornhuetter-Ferguson / Cape Cod reserving.")
    ap.add_argument("--db", type=Path, default=Path("data/state.db"))
    ap.add_argument("--insurer"); ap.add_argument("--lob"); ap.add_argument("--metric")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--premiums", help="a-priori basis, 'AY:premium,AY:premium,...'")
    ap.add_argument("--elr", type=float, help="a-priori expected loss ratio for BF")
    ap.add_argument("--cape-cod", action="store_true",
                    help="derive ELR from the triangle (Stanard-Bühlmann)")
    ap.add_argument("--tail", type=float, default=1.0)
    ap.add_argument("--min-credible", type=int, default=3)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run the worked example")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    apriori_override = None
    if args.demo:
        cells = [{"ay": c["ay"], "dev": c["dev"], "value": c["value"]} for c in _DEMO["cells"]]
        premiums = {int(k): float(v) for k, v in _DEMO["premiums"].items()}
        cli_elr = _DEMO["elr"]
        label = "DEMO (reference.md worked example)"
    elif args.stdin:
        cells, premiums, cli_elr = cells_from_stdin()
        label = "stdin triangle"
    else:
        if not (args.insurer and args.lob and args.metric):
            ap.error("provide --insurer, --lob, --metric (or --stdin / --demo).")
        cells, as_of = cells_from_db(args.db, args.insurer, args.lob, args.metric, args.as_of)
        premiums, cli_elr = parse_premiums(args.premiums), args.elr
        label = f"{args.insurer} / {args.lob} / {args.metric} @ {as_of}"

    if args.premiums and not args.demo:
        premiums = parse_premiums(args.premiums) or premiums
    if args.elr is not None:
        cli_elr = args.elr

    dev = develop(cells, tail=args.tail, min_credible=args.min_credible)
    cc = cape_cod_elr(dev["per_ay"], premiums) if premiums else None

    # ELR selection: --cape-cod forces the derived ELR; else a supplied --elr;
    # else fall back to Cape Cod if premiums are present; else there's no a-priori.
    if args.cape_cod and cc is not None:
        elr, elr_source = cc["elr"], "Cape Cod (derived)"
    elif cli_elr is not None:
        elr, elr_source = cli_elr, "supplied a-priori ELR"
    elif cc is not None:
        elr, elr_source = cc["elr"], "Cape Cod (derived)"
    else:
        sys.exit("Provide --elr, or --premiums (for --cape-cod), or a-priori ultimates. "
                 "BF needs an a-priori expected loss for the undeveloped part.")

    rows = apply_bf(dev["per_ay"], premiums, elr, apriori_override)

    if args.format == "json":
        print(json.dumps({"label": label, "elr": elr, "elr_source": elr_source,
                          "cape_cod": cc, "factors": dev["factors"], "cdf": dev["cdf"],
                          "per_ay": rows, "totals": totals(rows),
                          "warnings": dev["warnings"]}, indent=2))
    else:
        print(render_text(dev, rows, elr, elr_source, cc, label))


if __name__ == "__main__":
    main()
