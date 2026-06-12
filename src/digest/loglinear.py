"""Log-linear scoring weights (A3 stage 2) — learn the formula's exponents.

The leaderboard score is a product of factors, i.e. exactly a log-linear model
with every exponent fixed at 1:

    log S = Σᵢ wᵢ · log fᵢ ,   wᵢ ≡ 1  ⇔  today's heuristic, bit for bit.

This module learns the wᵢ from corroboration outcomes with a logistic fit whose
L2 penalty shrinks toward **w = 1** (not 0): the hand-tuned formula is the
credibility complement, and data moves an exponent away from 1 only when it has
the evidence — the Bühlmann posture applied to the formula itself.

Activation is deliberately slow and observable:

  1. `evaluate()` runs from the weekly `digest learn` job. Gates: ≥300 labeled
     rows, both classes present. Split: chronological + embargo (reuses
     learn._temporal_split). Pass = out-of-sample ranking AUC of the reweighted
     score beats the heuristic's AND the bootstrap CI of the AUC difference is
     clear of 0. Every evaluation is persisted to `loglinear_evals`.
  2. ELIGIBLE = the latest two consecutive evaluations both passed (the regime
     detector's hysteresis discipline).
  3. APPLIED only when the user also sets `loglinear: {apply: 1}` in Scoring
     Weights.md. Until then the heuristic stays authoritative and the gate
     history is just a report.

When applied, run_signals passes the learned exponents into score_item and the
score becomes Πᵢ fᵢ^wᵢ; the persisted factor columns stay raw so the exponents
remain auditable against them.
"""
from __future__ import annotations

import json
import logging

import numpy as np

from digest import db
from digest.learn import _temporal_split, auc, bootstrap_ci, is_backfill_row

logger = logging.getLogger(__name__)

# The formula's factors, in contract order (matches signal_scores columns).
FACTORS = [
    "source_mult", "regime_mult", "topic_relevance", "recency", "llm_judgment",
    "topic_boost", "burden_boost", "insurer_boost", "inflation_boost",
    "regulatory_boost", "tplf_boost", "reserve_boost",
]

MIN_LABELED = 300      # labeled rows before an evaluation is even attempted
# The historical backfill can supply MIN_LABELED rows overnight, but its mix is
# EDGAR-heavy by construction. Fine for per-source Bühlmann credibility; NOT a
# basis for re-weighting the pooled formula — so a pass additionally requires
# this many LIVE (non-backfill) labels. Until then evaluations still run and
# persist (the AUC trend is informative) but cannot pass.
MIN_LIVE_LABELED = 100
PASSES_REQUIRED = 2    # consecutive passing evaluations → eligible
_EPS = 1e-6            # factor floor before log (factors are ≥0.1 by design)
# Shrinkage toward w=1. Log-factors are small (|log f| ≲ 0.6), so the data
# gradient per unit weight is small too — λ=1 would cap every exponent near
# 1.1 no matter the evidence. λ=0.1 keeps a real prior pull while letting a
# genuinely informative factor reach w ≈ 2–3.
_L2 = 0.1


def _log_matrix(rows) -> np.ndarray:
    """X[i, j] = log fⱼ for row i; missing/NULL factors are neutral (log 1 = 0)."""
    out = np.zeros((len(rows), len(FACTORS)))
    for i, r in enumerate(rows):
        for j, f in enumerate(FACTORS):
            try:
                v = r[f]
            except (IndexError, KeyError):
                v = None
            out[i, j] = np.log(max(float(v), _EPS)) if v is not None else 0.0
    return out


def fit_weights(X: np.ndarray, y: np.ndarray,
                l2: float = _L2, lr: float = 0.1, iters: int = 2000) -> np.ndarray:
    """Logistic regression on log-factors with the penalty ‖w−1‖² — gradient
    descent from w=1, so zero signal leaves the heuristic untouched. Returns w
    (the bias is a ranking no-op and is fitted but discarded)."""
    n, d = X.shape
    w = np.ones(d)
    b = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -30, 30)))
        err = p - y
        w -= lr * (X.T @ err / n + l2 * (w - 1.0))
        b -= lr * float(err.mean())
    return w


def evaluate(horizon_days: int = 30, test_frac: float = 0.3, seed: int = 0) -> dict:
    """One gate evaluation: fit on the past, score the embargoed holdout, persist
    the verdict. Returns the summary (eval_id None under the small-n gates)."""
    rows = db.learning_dataset(horizon_days)
    n = len(rows)
    if n < MIN_LABELED:
        return {"eval_id": None, "n_samples": n,
                "note": f"need ≥{MIN_LABELED} labeled rows (have {n}) — heuristic stays"}
    y = np.array([int(r["corroborated"]) for r in rows], dtype=float)
    if len(set(y.tolist())) < 2:
        return {"eval_id": None, "n_samples": n,
                "note": "single-class outcomes — heuristic stays"}

    tr, te, split_note = _temporal_split(rows, test_frac, embargo_days=horizon_days)
    X = _log_matrix(rows)
    if len(set(y[tr].tolist())) < 2:
        return {"eval_id": None, "n_samples": n, "split": split_note,
                "note": "training split has a single class — heuristic stays"}

    w = fit_weights(X[tr], y[tr])
    s_weighted  = X[te] @ w                  # log of the reweighted score
    s_heuristic = X[te] @ np.ones(len(FACTORS))   # log of today's score
    y_te = y[te]

    auc_w = auc(y_te, s_weighted)
    auc_h = auc(y_te, s_heuristic)
    diff_ci = bootstrap_ci(
        lambda yy, idx: _auc_diff(yy, idx, s_weighted, s_heuristic),
        y_te, np.arange(len(y_te), dtype=float), seed=seed,
    )
    n_live = sum(1 for r in rows if not is_backfill_row(r))
    live_mix_ok = n_live >= MIN_LIVE_LABELED
    passed = (
        auc_w is not None and auc_h is not None and auc_w > auc_h
        and diff_ci is not None and diff_ci[0] > 0
        and live_mix_ok
    )
    weights_map = {f: round(float(x), 4) for f, x in zip(FACTORS, w)}
    eval_id = db.save_loglinear_eval({
        "horizon_days": horizon_days, "n_samples": n,
        "auc_weighted": auc_w, "auc_heuristic": auc_h,
        "diff_ci_low": diff_ci[0] if diff_ci else None,
        "diff_ci_high": diff_ci[1] if diff_ci else None,
        "passed": passed, "weights_json": json.dumps(weights_map),
    })
    summary = {
        "eval_id": eval_id, "n_samples": n, "n_live": n_live, "split": split_note,
        "auc_weighted": auc_w, "auc_heuristic": auc_h, "diff_ci": diff_ci,
        "passed": passed, "eligible": is_eligible(), "weights": weights_map,
    }
    if not live_mix_ok:
        summary["note"] = (
            f"live-mix gate: need ≥{MIN_LIVE_LABELED} live (non-backfill) labels "
            f"(have {n_live}) — backfill labels alone cannot re-weight the formula"
        )
    logger.info(
        "loglinear: eval #%d %s (AUC %.3f vs %.3f, diff CI %s, eligible=%s)",
        eval_id, "PASS" if passed else "fail",
        auc_w or 0.0, auc_h or 0.0, diff_ci, summary["eligible"],
    )
    return summary


def _auc_diff(y, idx, s_w, s_h):
    """AUC(weighted) − AUC(heuristic) on a bootstrap resample. `idx` rides
    through bootstrap_ci's score slot as float indices into the holdout."""
    ii = idx.astype(int)
    a_w, a_h = auc(y, s_w[ii]), auc(y, s_h[ii])
    if a_w is None or a_h is None:
        return None
    return a_w - a_h


def is_eligible() -> bool:
    """True when the latest PASSES_REQUIRED evaluations all passed."""
    evals = db.recent_loglinear_evals(n=PASSES_REQUIRED)
    return len(evals) == PASSES_REQUIRED and all(e["passed"] for e in evals)


def active_weights(cfg: dict | None) -> dict[str, float] | None:
    """The exponents run_signals should apply, or None (the default).

    Non-None only when BOTH the gate is eligible (two consecutive passes) AND
    the user set `loglinear: {apply: 1}` in Scoring Weights.md. The exponents
    come from the latest passing evaluation."""
    if not cfg or float(cfg.get("apply", 0.0)) < 1.0:
        return None
    if not is_eligible():
        return None
    latest = db.recent_loglinear_evals(n=1)
    try:
        return {k: float(v) for k, v in json.loads(latest[0]["weights_json"]).items()}
    except (IndexError, TypeError, ValueError, KeyError):
        return None
