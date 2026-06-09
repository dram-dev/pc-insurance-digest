"""Learned scorer (Databricks Option 4) — relevance from data, not hand-tuning.

A self-contained numpy logistic regression learns to predict **corroboration**
(the Option 1b outcome label) from the same features the heuristic uses — the 11
boost factors + the heuristic `score` itself + materiality. The heuristic stays
authoritative; the learned score runs *alongside* it and is A/B'd on a holdout
(top-N precision: heuristic vs learned).

Free-Edition design: trains on the Mac-mini CPU, no sklearn/scipy/pandas (numpy
only — already a dep). MLflow logging is optional (lazy import; skipped if
absent). The documented upgrade path is sklearn/AutoML + Model Serving +
ai_query() once off Free Edition.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import numpy as np

from digest import db

logger = logging.getLogger(__name__)

# Feature order is the model's contract — persisted with each model so inference
# matches training even if this list later grows.
FEATURES = [
    "score", "source_mult", "regime_mult", "topic_relevance", "recency",
    "llm_judgment", "topic_boost", "burden_boost", "insurer_boost",
    "inflation_boost", "regulatory_boost", "tplf_boost", "materiality_score",
]
# Neutral defaults for NULLs: multipliers→1.0; score/materiality→0.0.
_DEFAULTS = {f: 1.0 for f in FEATURES}
_DEFAULTS["score"] = 0.0
_DEFAULTS["materiality_score"] = 0.0


def row_to_features(row) -> list[float]:
    out = []
    for f in FEATURES:
        try:
            v = row[f]
        except (IndexError, KeyError):
            v = None
        out.append(float(v) if v is not None else _DEFAULTS[f])
    return out


# ── Model (numpy logistic regression) ────────────────────────────────────


class LogisticModel:
    """Standardized L2-regularized logistic regression via gradient descent."""

    def __init__(self, mean, std, weights, bias, features):
        self.mean = np.asarray(mean, dtype=float)
        self.std = np.asarray(std, dtype=float)
        self.weights = np.asarray(weights, dtype=float)
        self.bias = float(bias)
        self.features = list(features)

    @staticmethod
    def _sigmoid(z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    @classmethod
    def fit(cls, X, y, features, lr=0.1, iters=800, l2=0.01) -> "LogisticModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        mean = X.mean(axis=0)
        std = X.std(axis=0)
        std[std == 0] = 1.0
        Xs = (X - mean) / std
        n, d = Xs.shape
        w = np.zeros(d)
        b = 0.0
        for _ in range(iters):
            p = cls._sigmoid(Xs @ w + b)
            err = p - y
            w -= lr * (Xs.T @ err / n + l2 * w)
            b -= lr * float(err.mean())
        return cls(mean, std, w, b, features)

    def predict_proba(self, X) -> np.ndarray:
        Xs = (np.asarray(X, dtype=float) - self.mean) / self.std
        return self._sigmoid(Xs @ self.weights + self.bias)

    def to_json(self) -> str:
        return json.dumps({
            "mean": self.mean.tolist(), "std": self.std.tolist(),
            "weights": self.weights.tolist(), "bias": self.bias,
            "features": self.features,
        })

    @classmethod
    def from_json(cls, s: str) -> "LogisticModel":
        d = json.loads(s)
        return cls(d["mean"], d["std"], d["weights"], d["bias"], d["features"])


# ── Metrics (pure numpy) ──────────────────────────────────────────────────


def auc(y_true, y_score) -> float | None:
    """Rank-based ROC AUC; None if only one class present."""
    y = np.asarray(y_true)
    s = np.asarray(y_score, dtype=float)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    order = s.argsort()
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def precision_at_k(scores, labels, k: int) -> float | None:
    """Fraction of the top-k by `scores` that are positive."""
    if k <= 0 or len(scores) == 0:
        return None
    idx = np.argsort(-np.asarray(scores, dtype=float))[:k]
    return float(np.asarray(labels)[idx].mean())


def bootstrap_ci(
    metric_fn, y, s, n_boot: int = 500, seed: int = 0, alpha: float = 0.10,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI for a metric over a (labels, scores) holdout.
    Resamples rows with replacement; metric draws that come back None (e.g. a
    single-class resample for AUC) are skipped. None if no draw is computable.
    Default alpha=0.10 → a 5th–95th percentile interval."""
    y = np.asarray(y)
    s = np.asarray(s, dtype=float)
    n = len(y)
    if n == 0:
        return None
    rng = np.random.RandomState(seed)
    vals = []
    for _ in range(n_boot):
        idx = rng.randint(0, n, n)
        v = metric_fn(y[idx], s[idx])
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    lo, hi = np.percentile(vals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (round(float(lo), 3), round(float(hi), 3))


# ── Train / score ─────────────────────────────────────────────────────────


def _log_mlflow(params: dict, metrics: dict) -> None:
    """Best-effort MLflow logging (skipped if mlflow isn't installed)."""
    try:
        import mlflow  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        with mlflow.start_run(run_name="digest-learned-scorer"):
            mlflow.log_params(params)
            mlflow.log_metrics({k: v for k, v in metrics.items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        logger.warning("mlflow logging skipped: %s", exc)


def _temporal_split(
    rows, test_frac: float, embargo_days: int,
) -> tuple[list[int], list[int], str]:
    """Chronological holdout with an embargo purge — never a random split.

    Rows are ordered by ingested_at; the most recent `test_frac` is the holdout.
    A training item whose corroboration window (t, t+horizon] could overlap the
    holdout start leaks shared events into both sides, so training rows ingested
    within `embargo_days` of the holdout start are purged. When the purge
    starves training (<8 rows — thin early datasets where every item is recent),
    the un-purged chronological split is used and the relaxation is REPORTED in
    the returned note, never silent.

    Returns (train_indices, test_indices, split_note).
    """
    order = sorted(range(len(rows)), key=lambda i: str(rows[i]["ingested_at"] or ""))
    n_test = max(2, int(len(rows) * test_frac))
    test_idx, train_pool = order[-n_test:], order[:-n_test]

    test_start_raw = str(rows[test_idx[0]]["ingested_at"] or "")[:19].replace(" ", "T")
    try:
        cutoff = (datetime.fromisoformat(test_start_raw)
                  - timedelta(days=embargo_days)).isoformat()
    except ValueError:
        return train_pool, test_idx, "temporal (no parseable times — embargo skipped)"

    purged = [i for i in train_pool
              if str(rows[i]["ingested_at"] or "")[:19].replace(" ", "T") < cutoff]
    if len(purged) >= 8:
        return purged, test_idx, "temporal+embargo"
    return train_pool, test_idx, "temporal (embargo relaxed — thin history)"


def train(horizon_days: int = 30, test_frac: float = 0.3, seed: int = 42) -> dict:
    """Train a corroboration model from the labeled backtest set + A/B it vs the
    heuristic on a holdout. Returns a summary dict (model_id None if too little data).

    The holdout is CHRONOLOGICAL with an embargo of one horizon (a random split
    over time-ordered outcomes puts one news cycle on both sides and inflates
    every metric). `seed` drives the bootstrap CIs only."""
    rows = db.learning_dataset(horizon_days)
    n = len(rows)
    if n < 12:
        return {"model_id": None, "n_samples": n,
                "note": f"need ≥12 labeled items (have {n}); run more `digest outcomes` first"}

    X = np.array([row_to_features(r) for r in rows], dtype=float)
    y = np.array([int(r["corroborated"]) for r in rows], dtype=int)
    score_idx = FEATURES.index("score")

    tr, test, split_note = _temporal_split(rows, test_frac, embargo_days=horizon_days)
    if len(set(y[tr].tolist())) < 2:
        return {"model_id": None, "n_samples": n, "split": split_note,
                "note": "training split has a single class — need more varied outcomes"}

    model = LogisticModel.fit(X[tr], y[tr], FEATURES)
    proba = model.predict_proba(X[test])
    k = min(5, len(test))
    y_te = y[test]
    heur_te = X[test][:, score_idx]
    metrics = {
        "auc": auc(y_te, proba),
        "heuristic_precision": precision_at_k(heur_te, y_te, k),
        "learned_precision": precision_at_k(proba, y_te, k),
    }
    cis = {
        "auc_ci": bootstrap_ci(auc, y_te, proba, seed=seed),
        "learned_precision_ci": bootstrap_ci(
            lambda yy, ss: precision_at_k(ss, yy, k), y_te, proba, seed=seed),
        "heuristic_precision_ci": bootstrap_ci(
            lambda yy, ss: precision_at_k(ss, yy, k), y_te, heur_te, seed=seed),
    }
    model_id = db.save_learned_model({
        "target": "corroborated", "horizon_days": horizon_days, "n_samples": n,
        "auc": metrics["auc"], "heuristic_precision": metrics["heuristic_precision"],
        "learned_precision": metrics["learned_precision"],
        "features_json": json.dumps(FEATURES), "model_json": model.to_json(),
    })
    _log_mlflow({"horizon_days": horizon_days, "n_samples": n, "model": "logreg-numpy"}, metrics)
    return {"model_id": model_id, "n_samples": n, "k": k, "split": split_note,
            **metrics, **cis}


def score(model_id: int | None = None) -> dict:
    """Apply the latest (or given) model to all latest-scored items. Returns counts."""
    meta = db.learned_model_by_id(model_id) if model_id else db.latest_learned_model()
    if meta is None:
        return {"scored": 0, "note": "no trained model — run `digest learn` first"}
    model = LogisticModel.from_json(meta["model_json"])
    rows = db.items_to_learn_score()
    if not rows:
        return {"scored": 0, "model_id": meta["id"]}
    X = np.array([row_to_features(r) for r in rows], dtype=float)
    proba = model.predict_proba(X)
    for r, p in zip(rows, proba):
        db.upsert_learned_score(r["item_id"], meta["id"], float(p),
                                source=r["source"], source_id=r["source_id"])
    return {"scored": len(rows), "model_id": meta["id"]}


def run(horizon_days: int = 30) -> dict:
    """Train then score; also (re)fit the materiality calibrator from the same
    labeled set — it has its own (stricter) small-n gate and reports a note
    until it passes. Returns the merged summary."""
    summary = train(horizon_days=horizon_days)
    if summary.get("model_id"):
        summary["scored"] = score(summary["model_id"]).get("scored", 0)
    from digest.calibration import train_materiality_calibrator
    summary["calibrator"] = train_materiality_calibrator(horizon_days=horizon_days)
    return summary


def run_best(horizons: tuple[int, ...] = (30, 7)) -> dict:
    """Train on the first horizon with enough labeled data, so the loop self-starts
    before the longer (30d) horizon has matured. Returns that run's summary (or the
    last attempt's note if none had enough)."""
    summary: dict = {}
    for h in horizons:
        summary = run(horizon_days=h)
        if summary.get("model_id"):
            summary["horizon_days"] = h
            return summary
    return summary
