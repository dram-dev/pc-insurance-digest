#!/usr/bin/env python3
"""Indicated rate change — loss-ratio and pure-premium methods (Werner & Modlin).

Pure stdlib. Computes the overall indicated rate level change two equivalent ways:

  Loss-ratio method:
      indicated factor = (L&LAE ratio + fixed expense ratio)
                         / (1 − variable expense ratio − target UW profit)
      indicated change % = factor − 1

  Pure-premium method:
      indicated rate = (projected pure premium + fixed expense per exposure)
                       / (1 − variable expense ratio − target UW profit)
      indicated change % = indicated rate / current average rate − 1

Both can take the headline ratios directly, or the building blocks (reported
losses, premium/exposures, and the development / trend / on-level adjustments)
and assemble them. Feed it numbers pulled from a carrier's filings (EDGAR
content, investor supplements) or the severity tape for the trend.

  python ratemaking_indication.py --method loss_ratio \
      --loss-lae-ratio 0.65 --fixed-expense-ratio 0.06 \
      --variable-expense-ratio 0.25 --target-profit 0.05

  echo '{"method":"pure_premium","pure_premium":300,"fixed_expense_per_exposure":20,
         "variable_expense_ratio":0.25,"target_profit":0.05,"current_avg_rate":450}' \
      | python ratemaking_indication.py --stdin
"""
from __future__ import annotations

import argparse
import json
import sys


def _trend_factor(annual: float, years: float) -> float:
    return (1.0 + annual) ** years


def loss_ratio_method(p: dict) -> dict:
    """Loss-ratio indication. Accepts loss_lae_ratio directly, or losses+premium
    with optional development / trend / on-level adjustments."""
    if p.get("loss_lae_ratio") is not None:
        lr = float(p["loss_lae_ratio"])
        detail = {"loss_lae_ratio_input": lr}
    else:
        losses = float(p["losses"])
        premium = float(p["premium"])
        dev = float(p.get("development", 1.0))
        trend = _trend_factor(float(p.get("trend_annual", 0.0)),
                              float(p.get("trend_years", 0.0)))
        on_level = float(p.get("on_level", 1.0))
        ult = losses * dev * trend
        prem_crl = premium * on_level
        lr = ult / prem_crl
        detail = {"ultimate_loss_lae": round(ult, 4),
                  "premium_at_current_rate_level": round(prem_crl, 4),
                  "development": dev, "trend_factor": round(trend, 6),
                  "on_level": on_level}
    fixed = float(p["fixed_expense_ratio"])
    var = float(p["variable_expense_ratio"])
    profit = float(p["target_profit"])
    variable_perm = 1.0 - var - profit
    if variable_perm <= 0:
        sys.exit("variable expense ratio + target profit must be < 1.")
    factor = (lr + fixed) / variable_perm
    return {
        "method": "loss_ratio",
        "loss_lae_ratio": round(lr, 6),
        "fixed_expense_ratio": fixed,
        "variable_expense_ratio": var,
        "target_profit": profit,
        "permissible_loss_ratio": round(variable_perm, 6),
        "indicated_factor": round(factor, 6),
        "indicated_change_pct": round((factor - 1.0) * 100.0, 4),
        **detail,
    }


def pure_premium_method(p: dict) -> dict:
    """Pure-premium indication. Accepts pure_premium directly, or losses+exposures
    with optional development / trend adjustments; needs current_avg_rate for %."""
    if p.get("pure_premium") is not None:
        pp = float(p["pure_premium"])
        detail = {"pure_premium_input": pp}
    else:
        losses = float(p["losses"])
        exposures = float(p["exposures"])
        dev = float(p.get("development", 1.0))
        trend = _trend_factor(float(p.get("trend_annual", 0.0)),
                              float(p.get("trend_years", 0.0)))
        pp = (losses * dev * trend) / exposures
        detail = {"projected_ultimate_loss_lae": round(losses * dev * trend, 4),
                  "exposures": exposures, "development": dev,
                  "trend_factor": round(trend, 6)}
    fixed_pe = float(p["fixed_expense_per_exposure"])
    var = float(p["variable_expense_ratio"])
    profit = float(p["target_profit"])
    variable_perm = 1.0 - var - profit
    if variable_perm <= 0:
        sys.exit("variable expense ratio + target profit must be < 1.")
    indicated_rate = (pp + fixed_pe) / variable_perm
    out = {
        "method": "pure_premium",
        "projected_pure_premium": round(pp, 4),
        "fixed_expense_per_exposure": fixed_pe,
        "variable_expense_ratio": var,
        "target_profit": profit,
        "permissible_loss_ratio": round(variable_perm, 6),
        "indicated_rate": round(indicated_rate, 4),
        **detail,
    }
    if p.get("current_avg_rate") is not None:
        cur = float(p["current_avg_rate"])
        out["current_avg_rate"] = cur
        out["indicated_change_pct"] = round((indicated_rate / cur - 1.0) * 100.0, 4)
    return out


def render_text(r: dict) -> str:
    out = [f"Indicated rate change — {r['method']} method", "=" * 50]
    for k, v in r.items():
        if k == "method":
            continue
        label = k.replace("_", " ")
        if k.endswith("_pct"):
            out.append(f"  {label:<34} {v:+.2f}%")
        elif isinstance(v, float):
            out.append(f"  {label:<34} {v:,.4f}")
        else:
            out.append(f"  {label:<34} {v}")
    if "indicated_change_pct" in r:
        out += ["", f"  >>> INDICATED RATE CHANGE: {r['indicated_change_pct']:+.2f}%"]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Indicated rate change (ratemaking).")
    ap.add_argument("--method", choices=["loss_ratio", "pure_premium"])
    ap.add_argument("--stdin", action="store_true", help="read params as JSON from stdin")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    # loss-ratio
    ap.add_argument("--loss-lae-ratio", type=float)
    ap.add_argument("--losses", type=float); ap.add_argument("--premium", type=float)
    ap.add_argument("--development", type=float, default=1.0)
    ap.add_argument("--trend-annual", type=float, default=0.0)
    ap.add_argument("--trend-years", type=float, default=0.0)
    ap.add_argument("--on-level", type=float, default=1.0)
    ap.add_argument("--fixed-expense-ratio", type=float)
    ap.add_argument("--variable-expense-ratio", type=float)
    ap.add_argument("--target-profit", type=float)
    # pure-premium
    ap.add_argument("--pure-premium", type=float)
    ap.add_argument("--exposures", type=float)
    ap.add_argument("--fixed-expense-per-exposure", type=float)
    ap.add_argument("--current-avg-rate", type=float)
    args = ap.parse_args()

    if args.stdin:
        p = json.load(sys.stdin)
        method = p.get("method") or args.method
    else:
        p = {k: v for k, v in vars(args).items() if v is not None}
        method = args.method
    if method not in ("loss_ratio", "pure_premium"):
        ap.error("provide --method loss_ratio|pure_premium (or method in stdin JSON).")

    r = loss_ratio_method(p) if method == "loss_ratio" else pure_premium_method(p)
    print(json.dumps(r, indent=2) if args.format == "json" else render_text(r))


if __name__ == "__main__":
    main()
