"""Wiring of the two formerly-dormant signals into the leaderboard scorer:
reserve_deterioration_boost (Option 5) and learned_score (Option 4).

Both are neutral by default (no reserving data / no model) — these tests seed
the data to prove the wiring activates, and that it's off otherwise.
"""
from __future__ import annotations

import numpy as np
import pytest

from digest import db, learn, signals


def _kept_summarized(make_item, sid, title):
    db.upsert_items([make_item(source="rss", source_id=sid, title=title)])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
        conn.execute(
            "UPDATE items SET triage_decision='keep', summary='body', materiality_score=1.0 WHERE id=?",
            (iid,))
    return iid


def test_reserve_boost_neutral_without_data(fresh_db, make_item):
    iid = _kept_summarized(make_item, "p1", "Progressive reserve charge")
    signals.run_signals()
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT reserve_boost FROM signal_scores WHERE item_id=?", (iid,)
        ).fetchone()
    assert r["reserve_boost"] == 1.0          # no reserving_signals → neutral


def test_reserve_boost_applies_for_named_insurer(fresh_db, make_item):
    db.upsert_reserving_signal({
        "insurer": "PGR", "lob": "auto", "metric": "incurred", "as_of": "2026-05-01",
        "ultimate": 600.0, "latest": 500.0, "ibnr": 100.0, "prior_ibnr": 80.0,
        "deterioration_pct": 0.25, "direction": "adverse",
    })
    iid = _kept_summarized(make_item, "p1", "Progressive posts adverse reserve development")
    other = _kept_summarized(make_item, "o1", "Generic cyber market update")

    signals.run_signals()
    with db.get_conn() as conn:
        pgr = conn.execute("SELECT reserve_boost FROM signal_scores WHERE item_id=?", (iid,)).fetchone()
        oth = conn.execute("SELECT reserve_boost FROM signal_scores WHERE item_id=?", (other,)).fetchone()
    assert pgr["reserve_boost"] == pytest.approx(1.25, abs=0.01)   # 1 + 0.25, capped 1.3
    assert oth["reserve_boost"] == 1.0                              # no insurer match


def test_learned_score_null_without_model(fresh_db, make_item):
    iid = _kept_summarized(make_item, "p1", "Some item")
    signals.run_signals()
    with db.get_conn() as conn:
        r = conn.execute("SELECT learned_score FROM signal_scores WHERE item_id=?", (iid,)).fetchone()
    assert r["learned_score"] is None          # no trained model → NULL


def test_learned_score_populated_with_model(fresh_db, make_item, monkeypatch):
    # Fake a trained model (separable on the first feature) via the registry lookup.
    X = np.vstack([np.full((10, len(learn.FEATURES)), -1.0),
                   np.full((10, len(learn.FEATURES)), 1.0)])
    y = np.array([0] * 10 + [1] * 10)
    model = learn.LogisticModel.fit(X, y, learn.FEATURES)
    monkeypatch.setattr(db, "latest_learned_model", lambda target="corroborated": {
        "id": 7, "model_json": model.to_json(),
    })
    iid = _kept_summarized(make_item, "p1", "Some item")
    signals.run_signals()
    with db.get_conn() as conn:
        r = conn.execute("SELECT learned_score FROM signal_scores WHERE item_id=?", (iid,)).fetchone()
    assert r["learned_score"] is not None and 0.0 <= r["learned_score"] <= 1.0
