"""Wiring of the two formerly-dormant signals into the leaderboard scorer:
reserve_deterioration_boost (Option 5) and learned_score (Option 4).

Both are neutral by default (no reserving data / no model) — these tests seed
the data to prove the wiring activates, and that it's off otherwise.
"""
from __future__ import annotations

import numpy as np
import pytest

from digest import db, learn, reserving, signals
from digest.parse.pdf_tables import Table
from digest.parse.triangles import parse_triangle


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
        "ultimate": 600.0, "latest": 500.0, "ibnr": 100.0, "prior_ibnr": None,
        # 30/600 = 5% one-year incurred development → K×0.05 = 0.25 boost-scale severity.
        "cy_development": 30.0, "deterioration_pct": 0.05, "direction": "adverse",
    })
    iid = _kept_summarized(make_item, "p1", "Progressive posts adverse reserve development")
    other = _kept_summarized(make_item, "o1", "Generic cyber market update")

    signals.run_signals()
    with db.get_conn() as conn:
        pgr = conn.execute("SELECT reserve_boost FROM signal_scores WHERE item_id=?", (iid,)).fetchone()
        oth = conn.execute("SELECT reserve_boost FROM signal_scores WHERE item_id=?", (other,)).fetchone()
    assert pgr["reserve_boost"] == pytest.approx(1.25, abs=0.01)   # 1 + 0.25, capped 1.3
    assert oth["reserve_boost"] == 1.0                              # no insurer match


def _triangle_table(scale: float) -> Table:
    """The known {IBNR 94.5} triangle, all cells scaled by `scale`."""
    def s(v: float) -> str:
        return str(round(v * scale, 2))
    return Table(
        page=1,
        header=["Accident Year", "12", "24", "36"],
        rows=[
            ["2023", s(100), s(150), s(165)],
            ["2024", s(110), s(165), ""],
            ["2025", s(120), "", ""],
        ],
    )


def test_triangle_pipeline_activates_reserve_boost(fresh_db, make_item):
    """End-to-end Lead 6: PDF-table triangle → upsert → run_reserving → severity_map
    → score_item produces reserve_boost > 1.0 for the named insurer — from a SINGLE
    filing (one-year development read off the within-filing diagonal; no second
    snapshot needed). A generic item naming no flagged insurer stays neutral."""
    db.upsert_triangle_cells(parse_triangle(
        _triangle_table(1.0), insurer="PGR", lob="auto",
        metric="incurred", as_of="2026-03-31"))
    reserving.run_reserving()
    sev = db.reserving_severity_map()
    assert sev.get("PGR", 0.0) == pytest.approx(
        reserving.DEVELOPMENT_BOOST_K * (70.0 / 544.5), abs=1e-3)

    pgr = _kept_summarized(make_item, "p1", "Progressive posts adverse reserve development")
    other = _kept_summarized(make_item, "o1", "Generic cyber market update")
    signals.run_signals()
    with db.get_conn() as conn:
        pgr_boost = conn.execute(
            "SELECT reserve_boost FROM signal_scores WHERE item_id=?", (pgr,)).fetchone()["reserve_boost"]
        oth_boost = conn.execute(
            "SELECT reserve_boost FROM signal_scores WHERE item_id=?", (other,)).fetchone()["reserve_boost"]
    assert pgr_boost == pytest.approx(1.3, abs=0.02)    # severity 0.64 → capped at 1.3
    assert oth_boost == 1.0


def test_disclosure_tone_alone_activates_reserve_boost(fresh_db, make_item):
    """Lead 5: adverse reserve TONE (no triangle) fires reserve_deterioration_boost
    on its own, capped at 1 + LANG_SEVERITY_CAP (1.15) — below a confirmed
    triangle's 1.30 — and stays 1.0 for an item naming no flagged insurer."""
    from digest.disclosure import LANG_SEVERITY_CAP

    db.upsert_disclosure_sentiment({
        "insurer": "PGR", "period": "2026Q1", "as_of": "2026-02-15",
        "reserve_tone": "strengthening", "adverse_language_score": 1.0,
        "source_filing": "0001-26-1",
    })
    pgr = _kept_summarized(make_item, "p1", "Progressive flags reserve strengthening")
    other = _kept_summarized(make_item, "o1", "Generic cyber market update")

    signals.run_signals()
    with db.get_conn() as conn:
        pgr_boost = conn.execute(
            "SELECT reserve_boost FROM signal_scores WHERE item_id=?", (pgr,)).fetchone()["reserve_boost"]
        oth_boost = conn.execute(
            "SELECT reserve_boost FROM signal_scores WHERE item_id=?", (other,)).fetchone()["reserve_boost"]
    assert pgr_boost == pytest.approx(1.0 + LANG_SEVERITY_CAP, abs=0.01)   # 1.15, tone-only
    assert oth_boost == 1.0


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
