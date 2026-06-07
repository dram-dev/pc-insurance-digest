#!/usr/bin/env python3
"""Chain-ladder loss reserving — LDFs, CDFs, per-accident-year ultimates + IBNR,
with credibility diagnostics. Pure stdlib (no numpy/pandas) so it runs anywhere.

Volume-weighted (all-year) development factors, matching the production roll-up in
src/digest/reserving.py — so for a clean triangle the totals here equal what
`digest reserving` stores in reserving_signals; this script just also exposes the
per-AY breakdown and per-factor volatility the roll-up hides.

Two input modes:

  # From the warehouse (read-only), latest snapshot unless --as-of given:
  python chain_ladder.py --insurer PGR --lob personal_auto --metric incurred

  # From an ad-hoc triangle on stdin (sparse cells or a dense matrix):
  echo '{"cells":[{"ay":2019,"dev":0,"value":1000}, ...]}' \
      | python chain_ladder.py --stdin

Outputs a human table by default; pass --format json for a structured object.
A loss triangle is cumulative paid or incurred loss by accident year (rows) ×
development period (columns); only the upper-left half is observed.
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


def cells_from_stdin() -> list[dict]:
    """Accept {"cells":[{ay,dev,value}]} or {"accident_years","dev_periods","rows"}."""
    payload = json.load(sys.stdin)
    if "cells" in payload:
        return [{"ay": c["ay"], "dev": c["dev"], "value": c["value"]}
                for c in payload["cells"] if c.get("value") is not None]
    ays = payload["accident_years"]
    devs = payload["dev_periods"]
    cells = []
    for i, ay in enumerate(ays):
        for j, dev in enumerate(devs):
            v = payload["rows"][i][j]
            if v is not None:
                cells.append({"ay": ay, "dev": dev, "value": float(v)})
    return cells


# ── Core: volume-weighted chain-ladder ──────────────────────────────────────


def chain_ladder(cells: list[dict], tail: float = 1.0, min_credible: int = 3) -> dict:
    """Develop a cumulative triangle to ultimate. Returns a structured estimate.

    cells: list of {ay, dev, value}. tail: factor from the oldest observed dev to
    ultimate (default 1.0 = none). min_credible: factors based on fewer link
    ratios than this are flagged low-credibility.
    """
    if not cells:
        sys.exit("Empty triangle.")
    ays = sorted({c["ay"] for c in cells})
    devs = sorted({c["dev"] for c in cells})
    grid = {(c["ay"], c["dev"]): float(c["value"]) for c in cells}
    n_dev = len(devs)
    if n_dev < 2:
        sys.exit("Need at least two development periods to compute a factor.")

    warnings: list[str] = []

    # Age-to-age factors dev[j] → dev[j+1], volume-weighted across all AYs with both.
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
        simple = statistics.fmean(ratios) if ratios else 1.0
        cv = (statistics.stdev(ratios) / simple) if len(ratios) >= 2 and simple else None
        n = len(ratios)
        if n < min_credible:
            warnings.append(
                f"factor dev {d0}->{d1} rests on {n} link ratio(s) "
                f"(< {min_credible}) — low credibility."
            )
        if vw < 1.0:
            warnings.append(
                f"factor dev {d0}->{d1} = {vw:.4f} < 1.0 "
                f"(downward development — verify; common for salvage/subro or incurred releases)."
            )
        factors.append({
            "from_dev": d0, "to_dev": d1, "factor": round(vw, 6),
            "simple_avg": round(simple, 6),
            "cv": round(cv, 4) if cv is not None else None,
            "n_ratios": n, "individual_ratios": [round(r, 6) for r in ratios],
        })

    # CDF to ultimate per dev column (last observed column gets the tail only).
    cdf = [1.0] * n_dev
    cdf[n_dev - 1] = tail
    for j in range(n_dev - 2, -1, -1):
        cdf[j] = cdf[j + 1] * factors[j]["factor"]

    # Develop each accident year from its latest observed diagonal cell.
    projection = []
    latest_total = ult_total = ibnr_total = 0.0
    for ay in ays:
        observed = [j for j, d in enumerate(devs) if (ay, d) in grid]
        if not observed:
            continue
        last_j = observed[-1]
        latest = grid[(ay, devs[last_j])]
        f = cdf[last_j]
        ult = latest * f
        ibnr = ult - latest
        # An AY missing interior cells (gaps before its latest) breaks the
        # development assumption — flag it rather than silently developing.
        if len(observed) != last_j + 1:
            warnings.append(f"accident year {ay} has interior gaps — developed cell may be unreliable.")
        latest_total += latest
        ult_total += ult
        ibnr_total += ibnr
        projection.append({
            "accident_year": ay, "latest_dev": devs[last_j],
            "latest": round(latest, 2), "cdf": round(f, 6),
            "ultimate": round(ult, 2), "ibnr": round(ibnr, 2),
        })

    return {
        "accident_years": ays, "dev_periods": devs, "tail": tail,
        "factors": factors,
        "cdf": {devs[j]: round(cdf[j], 6) for j in range(n_dev)},
        "projection": projection,
        "totals": {
            "latest": round(latest_total, 2),
            "ultimate": round(ult_total, 2),
            "ibnr": round(ibnr_total, 2),
            "ibnr_pct_of_latest": round(ibnr_total / latest_total, 4) if latest_total else None,
        },
        "warnings": warnings,
    }


# ── Rendering ────────────────────────────────────────────────────────────────


def render_text(est: dict, label: str) -> str:
    out = [f"Chain-ladder estimate — {label}", "=" * 60, ""]
    out.append("Age-to-age development factors (volume-weighted):")
    out.append(f"  {'dev':>10} {'factor':>10} {'simple':>10} {'CV':>8} {'n':>4}")
    for f in est["factors"]:
        cv = f"{f['cv']:.3f}" if f["cv"] is not None else "  n/a"
        out.append(f"  {str(f['from_dev'])+'->'+str(f['to_dev']):>10} "
                   f"{f['factor']:>10.4f} {f['simple_avg']:>10.4f} {cv:>8} {f['n_ratios']:>4}")
    out += ["", "Per accident-year projection:"]
    out.append(f"  {'AY':>6} {'latest':>14} {'CDF':>9} {'ultimate':>14} {'IBNR':>14}")
    for p in est["projection"]:
        out.append(f"  {p['accident_year']:>6} {p['latest']:>14,.0f} {p['cdf']:>9.4f} "
                   f"{p['ultimate']:>14,.0f} {p['ibnr']:>14,.0f}")
    t = est["totals"]
    out += ["", "Totals:",
            f"  latest (current diagonal): {t['latest']:>16,.0f}",
            f"  ultimate:                  {t['ultimate']:>16,.0f}",
            f"  IBNR / unpaid:             {t['ibnr']:>16,.0f}"
            + (f"  ({t['ibnr_pct_of_latest']:.1%} of latest)"
               if t["ibnr_pct_of_latest"] is not None else "")]
    if est["warnings"]:
        out += ["", "⚠ Credibility / data warnings:"]
        out += [f"  - {w}" for w in est["warnings"]]
    out += ["", "Note: IBNR here = ultimate − latest. On a PAID triangle that is total",
            "unpaid (case + pure IBNR); on an INCURRED triangle it is pure IBNR (case",
            "reserves are already in the incurred figure). Cross-check against the",
            "pipeline's stored estimate in reserving_signals and prior-period IBNR for",
            "adverse vs favorable development."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Chain-ladder reserving over a loss triangle.")
    ap.add_argument("--db", type=Path, default=Path("data/state.db"),
                    help="SQLite warehouse path (default data/state.db).")
    ap.add_argument("--insurer"); ap.add_argument("--lob"); ap.add_argument("--metric")
    ap.add_argument("--as-of", default=None, help="Triangle snapshot; latest if omitted.")
    ap.add_argument("--tail", type=float, default=1.0, help="Tail factor to ultimate (default 1.0).")
    ap.add_argument("--min-credible", type=int, default=3,
                    help="Flag factors built on fewer link ratios than this (default 3).")
    ap.add_argument("--stdin", action="store_true", help="Read a JSON triangle from stdin.")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if args.stdin:
        cells, label = cells_from_stdin(), "stdin triangle"
    else:
        if not (args.insurer and args.lob and args.metric):
            ap.error("provide --insurer, --lob, --metric (or use --stdin).")
        cells, as_of = cells_from_db(args.db, args.insurer, args.lob, args.metric, args.as_of)
        label = f"{args.insurer} / {args.lob} / {args.metric} @ {as_of}"

    est = chain_ladder(cells, tail=args.tail, min_credible=args.min_credible)
    if args.format == "json":
        print(json.dumps({"label": label, **est}, indent=2))
    else:
        print(render_text(est, label))


if __name__ == "__main__":
    main()
