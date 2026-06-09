"""Alpha engine — learn forward, benchmark-relative insurer returns.

Ties the chain together: the `features.py` panel (digest data + signal scores)
is X; the **forward excess return** of each insurer vs a peer benchmark (IAK,
SPY fallback) is y. The model is an early-warning / alpha layer that rides
*alongside* the heuristic leaderboard — it never feeds it.

Modeling choices, tuned for small-n (14 names × ~2y) on Apple Silicon:

* **Primary learner: scikit-learn `HistGradientBoosting`** — a LightGBM-class
  gradient-boosted-trees model that needs no external library (LightGBM needs a
  `brew install libomp` dylib), trains in ms on CPU, and handles NaNs natively
  (so missing trailing-return features are honest, not imputed). LightGBM is an
  optional accelerator (lazy import; falls back to HistGB if it won't load).
* **Cross-sectional pooling** — one model across all insurers, not 14 thin
  per-name models.
* **Honest validation: purged + embargoed walk-forward** — train strictly in the
  past, with an embargo gap of one horizon so a training label can't peek into a
  test feature window. Random splits would leak.
* **Scorecard the model must beat:** out-of-sample **Information Coefficient**
  (rank corr of prediction vs realized excess) and a top-minus-bottom
  **long-short** return, reported against three baselines — zero, momentum, and
  signal-score-only. If it doesn't beat them, the surfacing layer says so.

Small-n discipline mirrors `learn.py`: explicit gates return a
`{"model_id": None, "note": ...}` summary rather than a bogus model.
"""
from __future__ import annotations

import json
import logging
import pickle
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from digest import db, features

logger = logging.getLogger(__name__)

def has_edge(ic, baseline_ic) -> bool:
    """True only when the model carries a real signal: IC must be POSITIVE and
    beat the best baseline. Being less-negative than a negative baseline is not
    an edge — this guards the surfacing from crying signal where there is none."""
    if ic is None or ic <= 0:
        return False
    return baseline_ic is None or ic > baseline_ic


def _matrix(df, cols) -> np.ndarray:
    """Feature matrix for the model: NaN → 0.0. Signal-absence is genuinely 0,
    and a fully-NaN column (e.g. learned-score before `digest learn` has run)
    would otherwise crash HistGradientBoosting's binning. Leakage-free — it's a
    constant fill, not a fitted statistic."""
    return df[cols].astype(float).fillna(0.0).to_numpy()


DEFAULT_HORIZON = 20          # forward trading days for the return label
MIN_LABELED = 60              # need at least this many labeled rows to train
MIN_FOLDS = 2                 # walk-forward folds for the backtest
LS_QUANTILE = 0.3             # top/bottom 30% for the long-short leg

# ── labels ──────────────────────────────────────────────────────────────────


def _on_or_before(closes: dict[str, float], d: str) -> float | None:
    prior = [x for x in sorted(closes) if x <= d]
    return closes[prior[-1]] if prior else None


def add_labels(
    panel: pd.DataFrame,
    prices: dict[str, dict[str, float]],
    benchmark: dict[str, float],
    horizon: int = DEFAULT_HORIZON,
) -> pd.DataFrame:
    """Attach `fwd_excess` (insurer − benchmark forward return) and `beats_peer`
    (excess ≥ its own trailing σ) to each panel row. Rows without a full forward
    window get NaN labels (dropped before training). Strictly forward-looking —
    this is the only place the future enters, and it never touches X."""
    out = panel.copy()
    fwd_excess: list[float] = []
    for _, r in out.iterrows():
        ticker, as_of = r["ticker"], r["as_of"]
        closes = prices.get(ticker, {})
        dates = sorted(closes)
        excess = np.nan
        if as_of in closes:
            idx = dates.index(as_of)
            if idx + horizon < len(dates):
                end = dates[idx + horizon]
                ins_ret = closes[end] / closes[as_of] - 1.0 if closes[as_of] > 0 else np.nan
                b0, b1 = _on_or_before(benchmark, as_of), _on_or_before(benchmark, end)
                bench_ret = (b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else 0.0
                excess = ins_ret - bench_ret
        fwd_excess.append(excess)
    out["fwd_excess"] = fwd_excess

    # Classifier label: did the excess clear the name's own trailing return σ?
    # vol_20d is a daily σ; scale to the horizon. NaN vol → fall back to the
    # cross-sectional sign (beats == positive excess).
    horizon_sigma = out["vol_20d"] * np.sqrt(horizon)
    out["beats_peer"] = np.where(
        horizon_sigma.notna() & (horizon_sigma > 0),
        (out["fwd_excess"] >= horizon_sigma).astype(float),
        (out["fwd_excess"] > 0).astype(float),
    )
    out.loc[out["fwd_excess"].isna(), "beats_peer"] = np.nan
    return out


# ── metrics ─────────────────────────────────────────────────────────────────


def information_coefficient(pred: np.ndarray, actual: np.ndarray) -> float | None:
    """Spearman rank correlation of prediction vs realized excess. None if <3
    valid pairs or no variance (rank corr undefined)."""
    mask = ~(np.isnan(pred) | np.isnan(actual))
    if mask.sum() < 3:
        return None
    p, a = pred[mask], actual[mask]
    pr, ar = _rankdata(p), _rankdata(a)
    if np.std(pr) == 0 or np.std(ar) == 0:
        return None
    return float(np.corrcoef(pr, ar)[0, 1])


def _rankdata(x: np.ndarray) -> np.ndarray:
    """Average ranks (ties shared), like scipy.stats.rankdata — kept numpy-only."""
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1)
    # average tied ranks
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    return (sums / counts)[inv]


def hit_rate(pred: np.ndarray, actual: np.ndarray) -> float | None:
    """Fraction of predictions whose sign matches the realized excess sign."""
    mask = ~(np.isnan(pred) | np.isnan(actual))
    if mask.sum() == 0:
        return None
    return float((np.sign(pred[mask]) == np.sign(actual[mask])).mean())


def long_short_return(pred: np.ndarray, actual: np.ndarray, q: float = LS_QUANTILE) -> float | None:
    """Mean realized excess of the top-`q` predictions minus the bottom-`q`."""
    mask = ~(np.isnan(pred) | np.isnan(actual))
    if mask.sum() < 4:
        return None
    p, a = pred[mask], actual[mask]
    k = max(1, int(len(p) * q))
    order = np.argsort(p)
    bottom, top = a[order[:k]], a[order[-k:]]
    return float(top.mean() - bottom.mean())


# ── walk-forward ────────────────────────────────────────────────────────────


def walk_forward_folds(dates: list[str], n_splits: int, embargo_days: int):
    """Yield (train_dates, test_dates) over a sorted unique date axis. Train is
    strictly before each test block, PURGED so no train label (as_of + horizon)
    overlaps the test block start. Expanding window."""
    uniq = sorted(set(dates))
    if len(uniq) < n_splits + 1:
        return
    fold_size = len(uniq) // (n_splits + 1)
    for k in range(1, n_splits + 1):
        test_start_i = k * fold_size
        test_block = uniq[test_start_i: test_start_i + fold_size] if k < n_splits else uniq[test_start_i:]
        if not test_block:
            continue
        test_start = test_block[0]
        purge_before = (datetime.fromisoformat(test_start) - timedelta(days=embargo_days)).strftime("%Y-%m-%d")
        train_block = [d for d in uniq[:test_start_i] if d < purge_before]
        if train_block and test_block:
            yield set(train_block), set(test_block)


# ── model wrappers ──────────────────────────────────────────────────────────


def _make_regressor():
    """Primary regressor — LightGBM if it loads, else sklearn HistGB."""
    try:
        import lightgbm as lgb  # noqa
        return lgb.LGBMRegressor(n_estimators=200, num_leaves=15,
                                 learning_rate=0.05, min_child_samples=10,
                                 subsample=0.8, reg_lambda=1.0, verbosity=-1), "lightgbm"
    except Exception:  # noqa: BLE001 — missing pkg or libomp → robust fallback
        from sklearn.ensemble import HistGradientBoostingRegressor
        return HistGradientBoostingRegressor(
            max_depth=3, learning_rate=0.05, max_iter=200,
            l2_regularization=1.0, min_samples_leaf=10), "histgb"


def _make_classifier():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.05, max_iter=200,
        l2_regularization=1.0, min_samples_leaf=10)


# ── train / backtest ────────────────────────────────────────────────────────


def _load_labeled(horizon: int):
    """Build the panel, attach labels, return (labeled_df, prices, benchmark)."""
    panel = features.build_panel()
    if panel.empty:
        return panel, {}, {}
    insurers = sorted(panel["ticker"].unique())
    prices = {t: db.price_closes(t) for t in insurers}
    benchmark = db.price_closes("IAK") or db.price_closes("SPY")
    labeled = add_labels(panel, prices, benchmark, horizon=horizon)
    labeled = labeled[labeled["fwd_excess"].notna()].reset_index(drop=True)
    return labeled, prices, benchmark


def backtest(horizon: int = DEFAULT_HORIZON, n_splits: int = 3) -> dict:
    """Honest walk-forward scorecard: pooled out-of-sample IC, hit-rate,
    long-short, vs zero / momentum / signal-only baselines. No model is saved."""
    labeled, _, _ = _load_labeled(horizon)
    n = len(labeled)
    if n < MIN_LABELED:
        return {"ok": False, "n": n,
                "note": f"need ≥{MIN_LABELED} labeled rows (have {n}); backfill prices/signals"}

    X_cols = features.FEATURE_COLUMNS
    oof_pred, oof_actual = [], []
    oof_mom, oof_sig = [], []
    folds = list(walk_forward_folds(labeled["as_of"].tolist(), n_splits, embargo_days=horizon))
    if len(folds) < MIN_FOLDS:
        return {"ok": False, "n": n, "note": "not enough date span for walk-forward folds"}

    for train_d, test_d in folds:
        tr = labeled[labeled["as_of"].isin(train_d)]
        te = labeled[labeled["as_of"].isin(test_d)]
        if tr.empty or te.empty or tr["fwd_excess"].std() == 0:
            continue
        model, _ = _make_regressor()
        model.fit(_matrix(tr, X_cols), tr["fwd_excess"].to_numpy())
        oof_pred.extend(model.predict(_matrix(te, X_cols)))
        oof_actual.extend(te["fwd_excess"].to_numpy())
        oof_mom.extend(te["mom_60d"].fillna(0.0).to_numpy())       # momentum baseline
        oof_sig.extend(te["sig_score_sum"].fillna(0.0).to_numpy())  # signal-only baseline

    pred, actual = np.array(oof_pred), np.array(oof_actual)
    mom, sig = np.array(oof_mom), np.array(oof_sig)
    if len(pred) == 0:
        return {"ok": False, "n": n, "note": "all folds degenerate (no label variance)"}

    base_ics = [ic for ic in (information_coefficient(mom, actual),
                              information_coefficient(sig, actual)) if ic is not None]
    return {
        "ok": True, "n": n, "horizon": horizon, "folds": len(folds),
        "oos_rows": len(pred),
        "ic": information_coefficient(pred, actual),
        "hit_rate": hit_rate(pred, actual),
        "long_short": long_short_return(pred, actual),
        "baseline_ic": max(base_ics) if base_ics else None,
        "momentum_ic": information_coefficient(mom, actual),
        "signal_ic": information_coefficient(sig, actual),
    }


def train(horizon: int = DEFAULT_HORIZON, n_splits: int = 3) -> dict:
    """Backtest, then (if enough data) fit a final model on ALL labeled rows and
    persist it with its scorecard. Returns the merged summary."""
    bt = backtest(horizon=horizon, n_splits=n_splits)
    if not bt.get("ok"):
        return {"model_id": None, **bt}

    labeled, _, _ = _load_labeled(horizon)
    X = _matrix(labeled, features.FEATURE_COLUMNS)
    reg, algo = _make_regressor()
    reg.fit(X, labeled["fwd_excess"].to_numpy())

    clf = None
    yb = labeled["beats_peer"].to_numpy()
    if len(np.unique(yb[~np.isnan(yb)])) == 2:           # both classes present
        clf = _make_classifier()
        clf.fit(X, yb.astype(int))

    blob = pickle.dumps({"reg": reg, "clf": clf})
    metrics = {k: bt.get(k) for k in ("ic", "hit_rate", "long_short", "baseline_ic",
                                      "momentum_ic", "signal_ic", "folds", "oos_rows")}
    model_id = db.save_return_model({
        "target": "excess_return", "horizon_days": horizon, "algo": algo,
        "n_samples": len(labeled), "ic": bt.get("ic"), "hit_rate": bt.get("hit_rate"),
        "baseline_ic": bt.get("baseline_ic"), "long_short_ret": bt.get("long_short"),
        "features_json": json.dumps(features.FEATURE_COLUMNS),
        "model_blob": blob, "model_json": None, "metrics_json": json.dumps(metrics),
    })
    return {"model_id": model_id, "algo": algo, "has_classifier": clf is not None, **bt}


# ── predict ─────────────────────────────────────────────────────────────────


def predict(model_id: int | None = None, horizon: int = DEFAULT_HORIZON) -> dict:
    """Score the latest as-of row per insurer with the latest (or given) model;
    persist forecasts. Returns counts."""
    meta = db.return_model_by_id(model_id) if model_id else db.latest_return_model("excess_return")
    if meta is None:
        return {"forecasts": 0, "note": "no trained model — run `digest forecast train` first"}

    bundle = pickle.loads(meta["model_blob"])
    feat_cols = json.loads(meta["features_json"])
    horizon = meta["horizon_days"]

    panel = features.build_panel()
    if panel.empty:
        return {"forecasts": 0, "note": "empty panel — backfill prices/signals first"}
    # Latest as-of row per ticker (the live signal).
    latest = panel.sort_values("as_of").groupby("ticker").tail(1).reset_index(drop=True)
    X = _matrix(latest, feat_cols)
    pred_excess = bundle["reg"].predict(X)
    pred_prob = bundle["clf"].predict_proba(X)[:, 1] if bundle.get("clf") is not None else [None] * len(latest)

    scored_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [{
        "ticker": r["ticker"], "as_of": r["as_of"], "horizon_days": horizon,
        "pred_excess": float(pe), "pred_prob": (float(pp) if pp is not None else None),
        "model_id": meta["id"], "scored_at": scored_at,
    } for (_, r), pe, pp in zip(latest.iterrows(), pred_excess, pred_prob)]
    db.upsert_return_forecasts(rows)
    return {"forecasts": len(rows), "model_id": meta["id"], "horizon": horizon}


def run(horizon: int = DEFAULT_HORIZON) -> dict:
    """Train then predict. Returns the merged summary."""
    summary = train(horizon=horizon)
    if summary.get("model_id"):
        summary["forecasts"] = predict(summary["model_id"]).get("forecasts", 0)
    return summary
