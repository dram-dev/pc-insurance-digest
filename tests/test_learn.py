"""Option 4 — learned scorer: model math, metrics, train/score loop."""
from __future__ import annotations

import numpy as np

from digest import db, learn


# ── model unit tests (clean synthetic data) ──────────────────────────────


def test_logistic_model_learns_separable():
    rng = np.random.RandomState(0)
    neg = rng.normal(-2.0, 0.3, size=(40, 2))
    pos = rng.normal(2.0, 0.3, size=(40, 2))
    X = np.vstack([neg, pos])
    y = np.array([0] * 40 + [1] * 40)
    m = learn.LogisticModel.fit(X, y, ["a", "b"])
    p = m.predict_proba(X)
    assert p[y == 1].mean() > 0.8 and p[y == 0].mean() < 0.2


def test_model_json_roundtrip():
    rng = np.random.RandomState(1)
    X = rng.normal(size=(30, 3))
    y = (X[:, 0] > 0).astype(int)
    m = learn.LogisticModel.fit(X, y, ["a", "b", "c"])
    m2 = learn.LogisticModel.from_json(m.to_json())
    assert np.allclose(m.predict_proba(X), m2.predict_proba(X))
    assert m2.features == ["a", "b", "c"]


def test_auc_and_precision():
    assert learn.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0     # perfect
    assert learn.auc([1, 1, 1], [0.5, 0.6, 0.7]) is None            # single class
    assert learn.precision_at_k([0.9, 0.8, 0.1], [1, 1, 0], 2) == 1.0


def test_row_to_features_defaults():
    row = {f: None for f in learn.FEATURES}
    feats = learn.row_to_features(row)
    # multipliers default 1.0; score + materiality default 0.0
    assert feats[learn.FEATURES.index("source_mult")] == 1.0
    assert feats[learn.FEATURES.index("score")] == 0.0
    assert feats[learn.FEATURES.index("materiality_score")] == 0.0


# ── train / score integration ─────────────────────────────────────────────


def _seed_labeled(make_item, n=30):
    for i in range(n):
        score = 0.5 + (i / n) * 2.5            # 0.5 .. 3.0
        sid = f"i{i}"
        db.upsert_items([make_item(source="rss", source_id=sid, title=f"t{i}")])
        with db.get_conn() as conn:
            iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
            conn.execute("UPDATE items SET triage_decision='keep' WHERE id=?", (iid,))
        db.upsert_signal_scores([{
            "item_id": iid, "computed_at": f"2026-01-01T00:00:{i:02d}", "score": score,
            "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0,
            "recency": 1.0, "llm_judgment": 1.0, "topic_boost": 1.0, "burden_boost": 1.0,
            "insurer_boost": 1.0, "inflation_boost": 1.0, "regulatory_boost": 1.0,
            "tplf_boost": 1.0, "tier": "high",
        }])
        db.upsert_backtest_outcome(iid, 30, {
            "corroborated": score > 1.7,                # learnable from `score`
            "signals": ["followon"] if score > 1.7 else [],
        })


def test_train_persists_model_and_metrics(fresh_db, make_item):
    _seed_labeled(make_item, n=30)
    summary = learn.train(horizon_days=30)
    assert summary["model_id"] is not None
    assert summary["n_samples"] == 30
    # model round-trips from the registry
    meta = db.latest_learned_model()
    assert meta["target"] == "corroborated"
    m = learn.LogisticModel.from_json(meta["model_json"])
    assert m.features == learn.FEATURES


def test_run_trains_then_scores(fresh_db, make_item):
    _seed_labeled(make_item, n=30)
    summary = learn.run(horizon_days=30)
    assert summary["model_id"] is not None
    assert summary["scored"] == 30
    with db.get_conn() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM learned_scores").fetchone()
        (rng,) = conn.execute(
            "SELECT (MIN(learned_score) >= 0 AND MAX(learned_score) <= 1) FROM learned_scores"
        ).fetchone()
    assert n == 30 and rng == 1                        # probabilities in [0,1]


def test_train_too_few_labels_is_graceful(fresh_db, make_item):
    _seed_labeled(make_item, n=5)
    summary = learn.train(horizon_days=30)
    assert summary["model_id"] is None and "need ≥12" in summary["note"]
