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


def _seed_labeled(make_item, n=30, horizon=30):
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
        db.upsert_backtest_outcome(iid, horizon, {
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


def test_run_best_falls_back_to_shorter_horizon(fresh_db, make_item):
    # Only 7d labels exist (30d not matured yet) — run_best should train on 7d.
    _seed_labeled(make_item, n=30, horizon=7)
    summary = learn.run_best(horizons=(30, 7))
    assert summary["model_id"] is not None
    assert summary["horizon_days"] == 7
    assert summary["scored"] == 30


def test_run_best_returns_note_when_no_horizon_has_data(fresh_db, make_item):
    summary = learn.run_best(horizons=(30, 7))
    assert summary.get("model_id") is None


# ── temporal split + embargo (the random-split leakage fix) ───────────────


def _rows_at(days: list[int]) -> list[dict]:
    return [{"ingested_at": f"2026-01-{d:02d} 00:00:00"} for d in days]


def test_temporal_split_is_chronological_even_on_shuffled_input():
    rows = _rows_at([14, 3, 28, 1, 21, 7, 25, 10, 17, 5, 23, 12])
    tr, te, _ = learn._temporal_split(rows, test_frac=0.3, embargo_days=0)
    test_times = {rows[i]["ingested_at"] for i in te}
    train_times = {rows[i]["ingested_at"] for i in tr}
    assert max(train_times) < min(test_times)        # holdout is strictly the future


def test_temporal_split_purges_embargo_window():
    rows = _rows_at(list(range(1, 29)))              # one row/day, 28 days
    tr, te, note = learn._temporal_split(rows, test_frac=0.3, embargo_days=7)
    assert note == "temporal+embargo"
    assert min(rows[i]["ingested_at"] for i in te) == "2026-01-21 00:00:00"
    cutoff = "2026-01-14 00:00:00"                              # test_start − 7d
    assert all(rows[i]["ingested_at"] < cutoff for i in tr)     # purged, not just earlier
    # The 7 days before the holdout (label windows overlap it) are in NEITHER set.
    excluded = set(range(len(rows))) - set(tr) - set(te)
    assert {rows[i]["ingested_at"][:10] for i in excluded} == {
        f"2026-01-{d:02d}" for d in range(14, 21)}


def test_temporal_split_relaxes_embargo_on_thin_history():
    rows = _rows_at([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])    # 12 days span
    tr, te, note = learn._temporal_split(rows, test_frac=0.3, embargo_days=30)
    assert "relaxed" in note                          # reported, never silent
    assert len(tr) + len(te) == len(rows)             # falls back to un-purged split


# ── bootstrap CI ──────────────────────────────────────────────────────────


def test_bootstrap_ci_tight_on_separable_auc():
    y = np.array([0] * 20 + [1] * 20)
    s = np.array([0.1] * 20 + [0.9] * 20)
    ci = learn.bootstrap_ci(learn.auc, y, s, n_boot=200, seed=1)
    assert ci == (1.0, 1.0)                           # every resample is perfect


def test_bootstrap_ci_orders_and_handles_empty():
    rng = np.random.RandomState(2)
    y = rng.randint(0, 2, 60)
    s = rng.uniform(size=60)
    ci = learn.bootstrap_ci(learn.auc, y, s, n_boot=200, seed=2)
    assert ci is not None and ci[0] <= ci[1]
    assert learn.bootstrap_ci(learn.auc, [], [], n_boot=10) is None


def test_train_reports_split_and_cis(fresh_db, make_item):
    _seed_labeled(make_item, n=30)
    summary = learn.train(horizon_days=30)
    assert summary["model_id"] is not None
    assert summary["split"].startswith("temporal")    # never a random split
    assert "learned_precision_ci" in summary
