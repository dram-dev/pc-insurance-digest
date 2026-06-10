"""Bühlmann–Straub source credibility — variance components, shrinkage, wiring."""
from __future__ import annotations

import math

import pytest

from digest import credibility, db, signals
from digest.credibility import (
    SourceCredibility,
    buhlmann_straub,
    credibility_table,
    implied_multiplier,
)


# ── variance components + shrinkage ───────────────────────────────────────


def test_big_source_keeps_its_experience_small_source_shrinks():
    # edgar: 200 obs at 60%; reddit: 5 obs at 100% (tiny sample, lucky streak).
    bs = buhlmann_straub({"edgar": (200, 120), "reddit": (5, 5), "rss": (200, 60)})
    assert bs["vhm"] > 0 and math.isfinite(bs["k"])
    assert bs["z"]["edgar"] > 0.9                     # big n → keeps its own rate
    assert bs["z"]["reddit"] < bs["z"]["edgar"]       # thin n → less credibility
    # reddit's credibility rate is pulled well off its raw 100% toward the mean.
    assert bs["cred"]["reddit"] < 0.9
    assert bs["cred"]["edgar"] == pytest.approx(0.6, abs=0.05)


def test_homogeneous_sources_get_zero_credibility():
    # All sources at the same rate: between-source spread is sampling noise,
    # VHM ≤ 0 → Z = 0 everywhere and everyone sits at the grand mean.
    bs = buhlmann_straub({"a": (50, 25), "b": (50, 25), "c": (50, 25)})
    assert bs["k"] == math.inf
    assert all(z == 0.0 for z in bs["z"].values())
    assert all(c == pytest.approx(0.5) for c in bs["cred"].values())


def test_single_source_degenerates_to_grand_mean():
    bs = buhlmann_straub({"only": (40, 10)})
    assert bs["z"]["only"] == 0.0                     # no between-source contrast
    assert bs["cred"]["only"] == pytest.approx(0.25)


def test_empty_stats():
    bs = buhlmann_straub({})
    assert bs["z"] == {} and bs["cred"] == {}


# ── implied multiplier ────────────────────────────────────────────────────


def test_implied_multiplier_dampens_and_clamps():
    # cred 1.2× the mean → ratio √1.2 ≈ 1.095 (γ=0.5 dampening), inside the clamp.
    assert implied_multiplier(1.0, 0.48, 0.4) == pytest.approx(math.sqrt(1.2), abs=0.01)
    # 2× the mean → √2 ≈ 1.41 → clamped to 1.25.
    assert implied_multiplier(1.0, 0.8, 0.4) == pytest.approx(1.25)
    # Far below the mean → clamped to 0.75.
    assert implied_multiplier(1.3, 0.01, 0.5) == pytest.approx(1.3 * 0.75)
    # Degenerate base rate → hand multiplier unchanged.
    assert implied_multiplier(1.3, 0.5, 0.0) == 1.3


def test_neutral_experience_means_hand_multiplier():
    assert implied_multiplier(1.2, 0.4, 0.4) == pytest.approx(1.2)


# ── table + apply path ────────────────────────────────────────────────────


def _seed_outcomes(make_item, source: str, n: int, positives: int, offset: int = 0):
    for i in range(n):
        sid = f"{source}-{offset + i}"
        db.upsert_items([make_item(source=source, source_id=sid, title=f"t {sid}")])
        with db.get_conn() as conn:
            iid = conn.execute(
                "SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
        db.upsert_backtest_outcome(iid, 30, {
            "corroborated": i < positives, "signals": ["followon"] if i < positives else [],
        })


def test_credibility_table_from_outcomes(fresh_db, make_item):
    _seed_outcomes(make_item, "edgar", 60, 40)
    _seed_outcomes(make_item, "reddit", 10, 1)
    rows = credibility_table(horizon_days=30)
    assert [r.source for r in rows] == ["edgar", "reddit"]     # descending n
    edgar, reddit = rows
    assert edgar.raw_rate == pytest.approx(40 / 60, abs=0.001)
    assert edgar.hand_mult == 1.3 and reddit.hand_mult == 0.7  # hand-set tiers
    assert 0 <= reddit.z < edgar.z <= 1
    # edgar corroborates above the book mean → implied ≥ hand; reddit below → ≤.
    assert edgar.implied_mult >= edgar.hand_mult
    assert reddit.implied_mult <= reddit.hand_mult


def test_credibility_table_empty_without_outcomes(fresh_db):
    assert credibility_table(horizon_days=30) == []
    assert credibility.adjusted_source_multipliers(signals._load_scoring_weights()) == {}


def test_run_signals_applies_credibility_only_when_flagged(fresh_db, make_item, monkeypatch):
    _seed_outcomes(make_item, "rss", 50, 5)        # rss corroborates poorly
    _seed_outcomes(make_item, "edgar", 50, 40)     # edgar corroborates well
    # A kept+summarized rss item to score.
    db.upsert_items([make_item(source="rss", source_id="live-1", title="some story")])
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE items SET triage_decision='keep', summary='s', materiality_score=1.0 "
            "WHERE source_id='live-1'")

    def _weights(apply: float):
        w = {k: dict(v) for k, v in signals._DEFAULT_WEIGHTS.items()}
        w["credibility"]["apply"] = apply
        return w

    monkeypatch.setattr(signals, "_load_scoring_weights", lambda: _weights(0.0))
    signals.run_signals()
    with db.get_conn() as conn:
        off = conn.execute(
            """SELECT s.source_mult FROM signal_scores s JOIN items i ON i.id = s.item_id
               WHERE i.source_id='live-1' ORDER BY s.computed_at DESC LIMIT 1""").fetchone()
    assert off["source_mult"] == 1.0               # hand-set rss multiplier

    monkeypatch.setattr(signals, "_load_scoring_weights", lambda: _weights(1.0))
    signals.run_signals()
    with db.get_conn() as conn:
        on = conn.execute(
            """SELECT s.source_mult FROM signal_scores s JOIN items i ON i.id = s.item_id
               WHERE i.source_id='live-1' ORDER BY s.computed_at DESC LIMIT 1""").fetchone()
    assert on["source_mult"] < 1.0                 # poor experience pulled it down


# ── weekly note section ───────────────────────────────────────────────────


def test_render_source_credibility_table():
    from digest.obsidian import _render_source_credibility_table

    rows = [
        SourceCredibility("edgar", 60, 0.6667, 0.91, 0.65, 1.3, 1.41),
        SourceCredibility("reddit", 10, 0.10, 0.35, 0.38, 0.7, 0.62),
    ]
    out = _render_source_credibility_table(rows)
    text = "\n".join(out)
    assert "Source Credibility (Bühlmann–Straub)" in text
    assert "| edgar | 60 | 0.67 | 0.91 | 0.65 | 1.30 | 1.41 ▲ |" in text
    assert "0.62 ▼" in text
    assert _render_source_credibility_table([]) == []
