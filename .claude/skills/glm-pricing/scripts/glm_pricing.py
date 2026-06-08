#!/usr/bin/env python3
"""GLM rating relativities — Poisson frequency / Gamma severity / Tweedie pure
premium, log link, via IRLS. Pure stdlib (no numpy/statsmodels).

A P&C pricing GLM models a log-linear multiplicative rating plan:

    log(mu_i) = offset_i + b0 + sum_j b_j * x_ij
    rate_i = exp(offset_i) * exp(b0) * PROD_j relativity_j

so each fitted coefficient becomes a multiplicative **relativity** exp(b_j) off a
baseline exp(b0). Families (all log link):
  - poisson  : claim COUNT ~ Poisson, offset = log(exposure)  → frequency relativities
  - gamma    : claim SEVERITY ~ Gamma, weight = claim count    → severity relativities
  - tweedie  : pure premium ~ Tweedie(1<p<2), weight = exposure → loss-cost relativities

Verification property this script is tested against: for a SINGLE categorical
predictor, a Poisson/Gamma log-link GLM exactly recovers the observed group rates
/ means (so exp(b_j) = group_j rate ÷ baseline rate).

Input is JSON (stdin or --data file):
  {"family":"poisson",
   "rows":[{"exposure":100,"count":10,"factors":{"region":"A","age":"young"}}, ...]}
For gamma use "severity" (+ optional "count" weight); tweedie uses
"pure_premium" (+ "exposure" weight).
"""
from __future__ import annotations

import argparse
import json
import math
import sys


# ── tiny linear algebra (solve symmetric system A b = y) ────────────────────


def solve(A: list[list[float]], y: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. A is n×n, y is n."""
    n = len(A)
    M = [row[:] + [y[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("singular design matrix (collinear factors?).")
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col] / pivval
            if f:
                M[r] = [a - f * b for a, b in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] for i in range(n)]


# ── design matrix ────────────────────────────────────────────────────────────


def build_design(rows: list[dict], factor_names: list[str]):
    """One-hot encode categoricals, dropping the first-seen level of each factor as
    the baseline. Returns (X, columns) where columns[0]='(intercept)'."""
    levels: dict[str, list[str]] = {}
    for f in factor_names:
        seen = []
        for r in rows:
            v = str(r["factors"][f])
            if v not in seen:
                seen.append(v)
        levels[f] = sorted(seen)  # baseline = first alphabetically, deterministic
    columns = ["(intercept)"]
    colmap: list[tuple[str, str]] = []
    for f in factor_names:
        for lv in levels[f][1:]:
            columns.append(f"{f}={lv}")
            colmap.append((f, lv))
    X = []
    for r in rows:
        row = [1.0]
        for (f, lv) in colmap:
            row.append(1.0 if str(r["factors"][f]) == lv else 0.0)
        X.append(row)
    return X, columns, levels


# ── IRLS ─────────────────────────────────────────────────────────────────────


def irls(X, y, offset, prior_w, family, var_power=1.5, max_iter=100, tol=1e-10):
    """Iteratively reweighted least squares for a log-link GLM."""
    n, p = len(X), len(X[0])
    # variance function V(mu)
    if family == "poisson":
        Vf = lambda mu: mu
    elif family == "gamma":
        Vf = lambda mu: mu * mu
    elif family == "tweedie":
        Vf = lambda mu: mu ** var_power
    else:
        sys.exit(f"unknown family {family}")

    b = [0.0] * p
    b[0] = math.log(max(sum(y) / max(sum(prior_w), 1e-9), 1e-6))  # intercept seed
    for _ in range(max_iter):
        # eta, mu
        eta = [offset[i] + sum(X[i][j] * b[j] for j in range(p)) for i in range(n)]
        mu = [math.exp(min(e, 700)) for e in eta]
        # log link: dmu/deta = mu ; working weight w = prior_w * mu^2 / V(mu)
        W = [prior_w[i] * (mu[i] * mu[i]) / max(Vf(mu[i]), 1e-12) for i in range(n)]
        # working response z = (eta - offset) + (y - mu)/mu
        z = [(eta[i] - offset[i]) + (y[i] - mu[i]) / max(mu[i], 1e-12) for i in range(n)]
        # weighted normal equations  (X' W X) b = X' W z
        XtWX = [[sum(X[i][a] * W[i] * X[i][c] for i in range(n)) for c in range(p)]
                for a in range(p)]
        XtWz = [sum(X[i][a] * W[i] * z[i] for i in range(n)) for a in range(p)]
        b_new = solve(XtWX, XtWz)
        if max(abs(b_new[j] - b[j]) for j in range(p)) < tol:
            b = b_new
            break
        b = b_new
    # deviance-ish residual sum for reporting (Poisson deviance if poisson)
    return b


def fit(payload: dict) -> dict:
    family = payload["family"]
    rows = payload["rows"]
    if not rows:
        sys.exit("no rows.")
    factor_names = list(rows[0]["factors"].keys())
    X, columns, levels = build_design(rows, factor_names)

    if family == "poisson":
        y = [float(r["count"]) for r in rows]
        if any(float(r["exposure"]) <= 0 for r in rows):
            sys.exit("glm: poisson exposure must be > 0 for every row (offset = log exposure).")
        offset = [math.log(float(r["exposure"])) for r in rows]
        prior_w = [1.0] * len(rows)
        target_label = "frequency (per exposure)"
    elif family == "gamma":
        y = [float(r["severity"]) for r in rows]
        offset = [0.0] * len(rows)
        prior_w = [float(r.get("count", 1.0)) for r in rows]
        target_label = "severity (per claim)"
    elif family == "tweedie":
        y = [float(r["pure_premium"]) for r in rows]
        offset = [0.0] * len(rows)
        prior_w = [float(r.get("exposure", 1.0)) for r in rows]
        target_label = "pure premium (per exposure)"
    else:
        sys.exit(f"unknown family {family}")

    b = irls(X, y, offset, prior_w, family, var_power=float(payload.get("var_power", 1.5)))
    base = math.exp(b[0])
    relativities = {columns[j]: round(math.exp(b[j]), 6) for j in range(1, len(columns))}
    return {
        "family": family, "target": target_label,
        "baseline_levels": {f: levels[f][0] for f in factor_names},
        "baseline_rate": round(base, 6),
        "coefficients": {columns[j]: round(b[j], 6) for j in range(len(columns))},
        "relativities": relativities,
        "n_rows": len(rows),
    }


def render_text(r: dict) -> str:
    out = [f"GLM rating relativities — {r['family']} ({r['target']})", "=" * 56,
           f"  baseline levels : {r['baseline_levels']}",
           f"  baseline rate   : {r['baseline_rate']:,.6f}", "", "  Relativities (multiplicative off baseline):"]
    for k, v in r["relativities"].items():
        out.append(f"    {k:<28} ×{v:.4f}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="GLM rating relativities (IRLS).")
    ap.add_argument("--data", help="JSON file; omit to read stdin.")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    payload = json.load(open(args.data)) if args.data else json.load(sys.stdin)
    try:
        r = fit(payload)
    except KeyError as exc:
        sys.exit(f"glm: missing required field {exc}.")
    except ValueError as exc:                       # e.g. solve()'s singular design
        sys.exit(f"glm: {exc}")
    print(json.dumps(r, indent=2) if args.format == "json" else render_text(r))


if __name__ == "__main__":
    main()
