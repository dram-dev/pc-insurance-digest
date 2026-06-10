"""Log-linear weight scaffold — w=1 identity, ridge-toward-1 fit, gate logic."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from digest import db, loglinear, signals
from digest.loglinear import FACTORS, _log_matrix, evaluate, fit_weights


class _Row(dict):
    def keys(self):
        return super().keys()


# ── w = 1 reproduces the heuristic ─────────────────────────────────────────


def test_log_matrix_w1_is_log_of_heuristic_score():
    row = _Row({f: 1.0 for f in FACTORS})
    row["source_mult"] = 1.3
    row["recency"] = 0.5
    row["tplf_boost"] = 1.4
    X = _log_matrix([row])
    assert X.shape == (1, len(FACTORS))
    assert X[0].sum() == pytest.approx(math.log(1.3 * 0.5 * 1.4))


def test_log_matrix_missing_factor_is_neutral():
    row = _Row({f: 1.0 for f in FACTORS})
    del row["reserve_boost"]
    assert _log_matrix([row])[0].sum() == 0.0


def test_score_item_exponents_w1_identity_and_zero_kills_factor():
    regime = type("_Regime", (), {"multiplier": 1.0})()
    row = _Row(
        id=1, source="edgar", topic="social_inflation",
        published_at=None, ingested_at="2026-06-09T00:00:00+00:00",
        materiality_score=1.2, metadata_json='{"ticker": "PGR"}',
    )
    plain = signals.score_item(row, regime)
    w1 = signals.score_item(row, regime, exponents={f: 1.0 for f in FACTORS})
    assert w1.score == pytest.approx(plain.score, rel=1e-6)   # w=1 ≡ heuristic
    no_topic = signals.score_item(row, regime, exponents={"topic_boost": 0.0})
    assert no_topic.score == pytest.approx(plain.score / plain.topic_boost, rel=1e-4)
    assert no_topic.topic_boost == plain.topic_boost          # factor persists RAW


# ── fit: shrinkage toward 1 ────────────────────────────────────────────────


def test_fit_weights_stays_at_one_with_no_signal():
    rng = np.random.RandomState(0)
    X = np.zeros((200, len(FACTORS)))                 # every factor neutral
    y = rng.randint(0, 2, 200).astype(float)
    w = fit_weights(X, y)
    assert np.allclose(w, 1.0)                        # zero evidence → exponents hold at 1


def test_fit_weights_moves_informative_factor():
    rng = np.random.RandomState(1)
    n = 400
    X = np.zeros((n, len(FACTORS)))
    j_tplf = FACTORS.index("tplf_boost")
    j_rec = FACTORS.index("recency")
    X[:, j_tplf] = np.where(rng.uniform(size=n) < 0.4, math.log(1.3), 0.0)
    X[:, j_rec] = np.log(rng.uniform(0.3, 1.0, n))    # pure noise
    y = (X[:, j_tplf] > 0).astype(float)              # outcomes follow tplf exactly
    w = fit_weights(X, y)
    assert w[j_tplf] > 1.5                            # informative factor amplified
    assert abs(w[j_rec] - 1.0) < abs(w[j_tplf] - 1.0)  # noise stays near the prior


# ── gate evaluation end-to-end ─────────────────────────────────────────────


def _seed_panel(make_item, n=400):
    """Labeled rows where corroboration is EXACTLY the tplf flag while the
    heuristic score is polluted by noisy source/recency factors — the
    reweighted score has a real, learnable edge."""
    rng = np.random.RandomState(7)
    for i in range(n):
        sid = f"ll{i}"
        db.upsert_items([make_item(source="rss", source_id=sid, title=f"t{i}")])
        tplf = 1.3 if rng.uniform() < 0.4 else 1.0
        src = float(rng.choice([0.7, 1.0, 1.3]))
        rec = float(rng.uniform(0.3, 1.0))
        with db.get_conn() as conn:
            iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
            conn.execute("UPDATE items SET triage_decision='keep', ingested_at=? WHERE id=?",
                         (f"2025-{6 + i // 200:02d}-{1 + (i // 7) % 28:02d} {i % 24:02d}:00:00", iid))
        db.upsert_signal_scores([{
            "item_id": iid, "computed_at": f"2026-01-01T00:00:{i % 60:02d}",
            "score": src * rec * tplf,
            "source_mult": src, "regime_mult": 1.0, "topic_relevance": 1.0,
            "recency": rec, "llm_judgment": 1.0, "topic_boost": 1.0,
            "burden_boost": 1.0, "insurer_boost": 1.0, "inflation_boost": 1.0,
            "regulatory_boost": 1.0, "tplf_boost": tplf, "tier": "low",
        }])
        db.upsert_backtest_outcome(iid, 30, {
            "corroborated": tplf > 1.0, "signals": [],
        })


def test_evaluate_gated_below_min_labeled(fresh_db, make_item):
    _seed_panel(make_item, n=50)
    out = evaluate(horizon_days=30)
    assert out["eval_id"] is None and "need ≥300" in out["note"]


def test_evaluate_passes_when_reweighting_has_real_edge(fresh_db, make_item):
    _seed_panel(make_item, n=400)
    out = evaluate(horizon_days=30)
    assert out["eval_id"] is not None
    assert out["auc_weighted"] > out["auc_heuristic"]
    assert out["passed"] is True
    assert out["weights"]["tplf_boost"] > 1.0         # the informative exponent grew
    # Evaluation persisted with its verdict + weights.
    evals = db.recent_loglinear_evals(n=1)
    assert evals[0]["passed"] == 1
    assert json.loads(evals[0]["weights_json"])["tplf_boost"] == out["weights"]["tplf_boost"]


# ── eligibility + apply flag ──────────────────────────────────────────────


def _fake_eval(passed: bool, weights=None):
    db.save_loglinear_eval({
        "horizon_days": 30, "n_samples": 400, "auc_weighted": 0.9,
        "auc_heuristic": 0.7, "diff_ci_low": 0.05, "diff_ci_high": 0.3,
        "passed": passed, "weights_json": json.dumps(weights or {"tplf_boost": 1.8}),
    })


def test_eligible_requires_two_consecutive_passes(fresh_db):
    assert loglinear.is_eligible() is False           # no history
    _fake_eval(True)
    assert loglinear.is_eligible() is False           # one pass isn't enough
    _fake_eval(True)
    assert loglinear.is_eligible() is True
    _fake_eval(False)                                 # a fail resets the streak
    assert loglinear.is_eligible() is False


def test_active_weights_requires_gate_and_user_flag(fresh_db):
    _fake_eval(True)
    _fake_eval(True)
    assert loglinear.active_weights({"apply": 0.0}) is None    # eligible, not opted in
    assert loglinear.active_weights(None) is None
    w = loglinear.active_weights({"apply": 1.0})
    assert w == {"tplf_boost": 1.8}                            # eligible + opted in


def test_active_weights_none_when_not_eligible(fresh_db):
    _fake_eval(True)                                  # only one pass
    assert loglinear.active_weights({"apply": 1.0}) is None
