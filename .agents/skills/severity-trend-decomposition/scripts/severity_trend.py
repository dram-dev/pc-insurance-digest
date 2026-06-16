#!/usr/bin/env python3
"""Loss-cost trend — fit an annual exponential trend to a severity series and
decompose pure-premium trend into frequency × severity. Pure stdlib.

Method:
  - Fit log-linear OLS:  ln(value) = a + b·t   (t in years from the first point).
    Annualized trend = exp(b) − 1; report R² (on the log scale) and the latest
    point's deviation from the fitted trend (a quick "running hot/cold" read).
  - Decompose:  pure-premium trend = (1 + freq_trend)(1 + sev_trend) − 1.

DB mode reads the warehouse read-only (severity_index, the blended loss-cost tape):
  python severity_trend.py --db data/state.db --index-name blended_severity
  python severity_trend.py --db data/state.db --list           # show available indices

stdin mode (ad-hoc series, or a decomposition):
  echo '{"series":[{"date":"2020-01-01","value":100},{"date":"2021-01-01","value":106}]}' \
      | python severity_trend.py --stdin
  echo '{"frequency":[...],"severity":[...]}' | python severity_trend.py --stdin
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date


def _to_years(dates: list[str]) -> list[float]:
    d0 = date.fromisoformat(dates[0][:10])
    return [(date.fromisoformat(d[:10]) - d0).days / 365.25 for d in dates]


def fit_trend(series: list[dict]) -> dict:
    """Log-linear OLS over [{date,value}]. Returns annual trend, R², latest dev."""
    pts = [(s["date"], v) for s in series if (v := float(s["value"])) > 0]
    pts.sort(key=lambda x: x[0])
    if len(pts) < 3:
        sys.exit("need >=3 positive points to fit a trend.")
    dates = [p[0] for p in pts]
    t = _to_years(dates)
    yv = [math.log(p[1]) for p in pts]
    n = len(t)
    tbar = sum(t) / n
    ybar = sum(yv) / n
    sxx = sum((ti - tbar) ** 2 for ti in t)
    sxy = sum((t[i] - tbar) * (yv[i] - ybar) for i in range(n))
    b = sxy / sxx
    a = ybar - b * tbar
    # R² on the log scale
    ss_tot = sum((yi - ybar) ** 2 for yi in yv)
    ss_res = sum((yv[i] - (a + b * t[i])) ** 2 for i in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    fitted_last = math.exp(a + b * t[-1])
    actual_last = pts[-1][1]
    return {
        "n_points": n, "first_date": dates[0], "last_date": dates[-1],
        "annual_trend_pct": round((math.exp(b) - 1.0) * 100.0, 4),
        "r_squared": round(r2, 4),
        "fitted_last": round(fitted_last, 4), "actual_last": round(actual_last, 4),
        "last_vs_trend_pct": round((actual_last / fitted_last - 1.0) * 100.0, 4),
    }


def decompose(freq_trend_pct: float, sev_trend_pct: float) -> dict:
    f, s = freq_trend_pct / 100.0, sev_trend_pct / 100.0
    pp = (1 + f) * (1 + s) - 1.0
    return {
        "frequency_trend_pct": round(freq_trend_pct, 4),
        "severity_trend_pct": round(sev_trend_pct, 4),
        "pure_premium_trend_pct": round(pp * 100.0, 4),
    }


def db_series(db_path: str, index_name: str) -> tuple[list[dict], dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT observation_date AS date, value, zscore_12m FROM severity_index "
            "WHERE index_name=? AND value IS NOT NULL ORDER BY observation_date",
            (index_name,),
        ).fetchall()
        latest_z = rows[-1]["zscore_12m"] if rows else None
    finally:
        conn.close()
    return [{"date": r["date"], "value": r["value"]} for r in rows], {
        "index_name": index_name, "latest_zscore_12m": latest_z}


def list_indices(db_path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT index_name, category, COUNT(*) n, MIN(observation_date) first, "
            "MAX(observation_date) last FROM severity_index GROUP BY index_name "
            "ORDER BY n DESC")]
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Loss-cost severity trend + decomposition.")
    ap.add_argument("--db", default="data/state.db")
    ap.add_argument("--index-name")
    ap.add_argument("--list", action="store_true", help="list severity_index series")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if args.list:
        idx = list_indices(args.db)
        print(json.dumps(idx, indent=2))
        if not idx:
            print("(severity_index is empty — populate via `digest severity-tape`, "
                  "or use --stdin.)", file=sys.stderr)
        return

    meta: dict = {}
    if args.stdin:
        payload = json.load(sys.stdin)
        if "frequency" in payload and "severity" in payload:
            f = fit_trend(payload["frequency"])
            s = fit_trend(payload["severity"])
            r = {"frequency_fit": f, "severity_fit": s,
                 "decomposition": decompose(f["annual_trend_pct"], s["annual_trend_pct"])}
            print(json.dumps(r, indent=2))
            return
        series = payload.get("series")
        if series is None:
            sys.exit("stdin: provide 'series', or 'frequency' + 'severity'.")
    elif args.index_name:
        series, meta = db_series(args.db, args.index_name)
        if not series:
            sys.exit(f"no severity_index rows for index_name={args.index_name!r} "
                     f"(try --list, or --stdin).")
    else:
        ap.error("provide --index-name, --list, or --stdin.")

    r = {**meta, **fit_trend(series)}
    if args.format == "json":
        print(json.dumps(r, indent=2))
    else:
        out = [f"Loss-cost trend — {meta.get('index_name','stdin series')}", "=" * 48]
        for k, v in r.items():
            lab = k.replace("_", " ")
            out.append(f"  {lab:<22} {round(v, 2) + 0.0:+.2f}%" if k.endswith("_pct")
                       else f"  {lab:<22} {v}")
        print("\n".join(out))


if __name__ == "__main__":
    main()
