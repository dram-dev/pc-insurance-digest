"""The mobile-first Obsidian Brief note (brief.py).

Distinct from test_brief.py, which covers the console `digest brief` alert
watchlist. This covers the vault note: regime one-liner + vitals, the
per-topic-capped top picks, backend-error suppression, and the file write.
"""
from __future__ import annotations

import json

from digest import brief, db, obsidian


def _put_regime(cycle="transitioning_to_hard", cat="post_major_event",
                mult=1.32, evidence="Reinsurance pricing softening at June renewals."):
    db.upsert_regime_signal(
        as_of="2026-06-13T04:00:00+00:00",
        market_cycle=cycle, cat_load=cat,
        market_cycle_mult=1.1, cat_load_mult=1.2, multiplier=mult,
        evidence_json=json.dumps({"market_judgment": {"evidence": evidence}}),
        source="detector",
    )


def _put_blended_severity(value=140.20, z=0.50):
    db.upsert_severity_index([
        {"index_name": "blended_severity", "observation_date": "2026-05-01",
         "value": value, "zscore_12m": z, "is_anomaly": 0, "category": "blended",
         "source": "fred", "fetched_at": "2026-05-02"},
    ])


# ── Pure: top-pick selection ─────────────────────────────────────────────────

def test_top_picks_respects_per_topic_cap_and_limit():
    rows = [{"topic": "regulatory_rate", "score": s} for s in (9, 8, 7, 6)]
    rows += [{"topic": "social_inflation", "score": s} for s in (5, 4)]
    rows += [{"topic": "reserving", "score": 3}]
    picks = brief._top_picks(rows)
    topics = [p["topic"] for p in picks]
    assert topics.count("regulatory_rate") == brief.PER_TOPIC_CAP   # capped at 2
    assert len(picks) <= brief.TOP_PICKS
    assert topics == ["regulatory_rate", "regulatory_rate",
                      "social_inflation", "social_inflation", "reserving"]


# ── Regime one-liner + vitals ────────────────────────────────────────────────

def test_brief_regime_and_vitals_show_zscore_not_raw_level(fresh_db):
    _put_regime()
    _put_blended_severity(value=140.20, z=0.50)
    text, _ = brief.render_brief_note("2026-06-13")
    assert "📡" in text and "Transitioning To Hard" in text and "×1.32" in text
    assert "Reinsurance pricing softening" in text           # evidence one-liner
    assert "severity +0.50σ" in text                         # the z, not the level
    assert "140" not in text                                 # raw level never shown


def test_brief_suppresses_backend_error_evidence(fresh_db):
    _put_regime(evidence="backend error: MLX server not reachable at http://localhost:8080")
    text, _ = brief.render_brief_note("2026-06-13")
    assert "📡" in text                                       # regime line still renders
    assert "backend error" not in text and "not reachable" not in text


# ── File write ───────────────────────────────────────────────────────────────

def test_write_brief_note_writes_dated_file(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(obsidian.settings, "obsidian_vault_path", str(tmp_path))
    paths = obsidian.Paths.resolve()
    paths.ensure()
    target, n_picks = brief.write_brief_note("2026-06-13", paths)
    assert target.exists()
    assert target.name == "2026-06-13 Brief.md"
    assert target.parent.name == "Brief"
    body = target.read_text(encoding="utf-8")
    assert "kind: digest-brief" in body
    assert "# ⚡ Brief — 2026-06-13" in body
    assert n_picks == 0                                       # empty db → no picks
