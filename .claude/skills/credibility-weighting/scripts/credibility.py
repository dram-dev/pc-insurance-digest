#!/usr/bin/env python3
"""Credibility weighting — classical (limited-fluctuation) and Bühlmann.

Pure stdlib. Blends an observed estimate with a complement of credibility:

    estimate = Z * observed + (1 − Z) * complement

Classical (limited fluctuation):
    full-credibility standard  n_full = (z_(1+p)/2 / k)^2   claims  (Poisson freq)
    partial credibility        Z = min(1, sqrt(n / n_full))

Bühlmann (greatest accuracy / least-squares credibility):
    Z = N / (N + K),   K = EPV / VHM
  where EPV = expected process variance, VHM = variance of hypothetical means.
  Give K directly (--epv/--vhm), or estimate it empirically from grouped data
  (balanced Bühlmann): EPV = mean within-group variance; VHM = between-group
  variance of means − EPV/N.

  python credibility.py --mode classical --n 271 --p 0.9 --k 0.05 \
      --observed 0.80 --complement 0.65

  echo '{"mode":"buhlmann","groups":[[10,12],[20,18]]}' \
      | python credibility.py --stdin
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys


def full_credibility_standard(p: float, k: float) -> float:
    """n_full = (z_{(1+p)/2} / k)^2 — claims for full credibility, Poisson frequency."""
    z = statistics.NormalDist().inv_cdf((1.0 + p) / 2.0)
    return (z / k) ** 2


def classical(n: float, observed: float | None, complement: float | None,
              p: float, k: float, full: float | None) -> dict:
    n_full = full if full is not None else full_credibility_standard(p, k)
    z = min(1.0, (n / n_full) ** 0.5)
    out = {"mode": "classical", "n": n, "n_full": round(n_full, 2),
           "Z": round(z, 6)}
    if full is None:
        out["p"], out["k"] = p, k
    if observed is not None and complement is not None:
        out["observed"], out["complement"] = observed, complement
        out["credibility_weighted_estimate"] = round(
            z * observed + (1 - z) * complement, 6)
    return out


def buhlmann_from_k(n: float, k: float, observed: float | None,
                    complement: float | None) -> dict:
    z = n / (n + k)
    out = {"mode": "buhlmann", "N": n, "K": round(k, 6), "Z": round(z, 6)}
    if observed is not None and complement is not None:
        out["observed"], out["complement"] = observed, complement
        out["credibility_weighted_estimate"] = round(
            z * observed + (1 - z) * complement, 6)
    return out


def buhlmann_empirical(groups: list[list[float]]) -> dict:
    """Balanced Bühlmann: estimate EPV, VHM, K from r groups of equal size n.

    EPV = mean of within-group sample variances (denominator n−1).
    VHM = sample variance of group means (denominator r−1) − EPV/n.
    """
    r = len(groups)
    if r < 2:
        sys.exit("need >=2 groups for empirical Bühlmann.")
    n = len(groups[0])
    if any(len(g) != n for g in groups):
        sys.exit("balanced Bühlmann needs equal group sizes; use --epv/--vhm otherwise.")
    if n < 2:
        sys.exit("need >=2 observations per group to estimate within-group variance.")
    means = [statistics.fmean(g) for g in groups]
    grand = statistics.fmean(means)
    epv = statistics.fmean([statistics.variance(g) for g in groups])
    vhm = statistics.variance(means) - epv / n
    if vhm <= 0:
        # Groups indistinguishable beyond process noise → no credibility to the group.
        z, k = 0.0, float("inf")
        cred = {f"group_{i}": round(grand, 6) for i in range(r)}
    else:
        k = epv / vhm
        z = n / (n + k)
        cred = {f"group_{i}": round(z * means[i] + (1 - z) * grand, 6) for i in range(r)}
    return {
        "mode": "buhlmann_empirical", "groups": r, "obs_per_group": n,
        "group_means": [round(m, 6) for m in means], "grand_mean": round(grand, 6),
        "EPV": round(epv, 6), "VHM": round(vhm, 6),
        "K": (round(k, 6) if vhm > 0 else None), "Z": round(z, 6),
        "credibility_weighted": cred,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Credibility weighting.")
    ap.add_argument("--mode", choices=["classical", "buhlmann"])
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="json")
    ap.add_argument("--n", type=float)
    ap.add_argument("--p", type=float, default=0.9)
    ap.add_argument("--k", type=float, default=0.05)
    ap.add_argument("--full", type=float)
    ap.add_argument("--epv", type=float); ap.add_argument("--vhm", type=float)
    ap.add_argument("--observed", type=float); ap.add_argument("--complement", type=float)
    args = ap.parse_args()

    if args.stdin:
        p = json.load(sys.stdin)
        mode = p.get("mode") or args.mode
    else:
        p = {k: v for k, v in vars(args).items() if v is not None}
        mode = args.mode

    if mode == "classical":
        r = classical(p["n"], p.get("observed"), p.get("complement"),
                      p.get("p", 0.9), p.get("k", 0.05), p.get("full"))
    elif mode == "buhlmann":
        if "groups" in p:
            r = buhlmann_empirical(p["groups"])
        elif p.get("epv") is not None and p.get("vhm") is not None:
            k = p["epv"] / p["vhm"]
            r = buhlmann_from_k(p["n"], k, p.get("observed"), p.get("complement"))
        elif p.get("k") is not None and p.get("n") is not None and args.mode == "buhlmann":
            r = buhlmann_from_k(p["n"], p["k"], p.get("observed"), p.get("complement"))
        else:
            sys.exit("buhlmann: give --groups (stdin), or --epv & --vhm, or --n & --k.")
    else:
        ap.error("provide --mode classical|buhlmann (or mode in stdin JSON).")

    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
