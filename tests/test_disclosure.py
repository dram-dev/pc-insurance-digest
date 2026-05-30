"""Disclosure-sentiment reserve-tone parser + severity blend (EKG Lead 5).

Network-free: drives the lexicon and the severity-map blend directly, plus one
end-to-end run over a synthetic stored EDGAR item.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from digest import db
from digest.disclosure import (
    LANG_SEVERITY_CAP,
    language_severity,
    run_disclosure,
    score_filing,
)

_ADVERSE = ("The company reported unfavorable prior-year reserve development and "
            "recorded reserve strengthening of $120 million in its auto line.")
_FAVORABLE = ("Results benefited from favorable prior-year development and net "
              "reserve releases across the homeowners book.")
_NEUTRAL = "Net premiums written rose 5% on strong personal-auto retention."


# ── lexicon ──────────────────────────────────────────────────────────────────

def test_adverse_text_reads_strengthening():
    tone, score = score_filing(_ADVERSE)
    assert tone == "strengthening"
    assert score > 0


def test_favorable_text_reads_releasing_with_zero_adverse_score():
    tone, score = score_filing(_FAVORABLE)
    assert tone == "releasing"
    assert score == 0.0


def test_no_reserve_discussion_is_neutral():
    assert score_filing(_NEUTRAL) == ("neutral", 0.0)
    assert score_filing("") == ("neutral", 0.0)


def test_scoring_is_deterministic():
    assert score_filing(_ADVERSE) == score_filing(_ADVERSE)


def test_mixed_tone_nets_out():
    # One adverse + one favorable phrase → balanced → neutral, score 0.
    mixed = "reserve strengthening in auto was offset by favorable development in home"
    tone, score = score_filing(mixed)
    assert tone == "neutral"
    assert score == 0.0


# ── language_severity scaling ────────────────────────────────────────────────

def test_language_severity_caps_and_gates():
    assert language_severity("strengthening", 1.0) == pytest.approx(LANG_SEVERITY_CAP)
    assert language_severity("strengthening", 0.5) == pytest.approx(LANG_SEVERITY_CAP / 2)
    assert language_severity("releasing", 1.0) == 0.0     # favorable tone never boosts
    assert language_severity("neutral", 1.0) == 0.0
    assert language_severity("strengthening", 0.0) == 0.0


# ── severity-map blend (the wiring into the existing boost) ───────────────────

def _adverse_disclosure(insurer="PGR", score=1.0):
    db.upsert_disclosure_sentiment({
        "insurer": insurer, "period": "2026Q1", "as_of": "2026-02-15",
        "reserve_tone": "strengthening", "adverse_language_score": score,
        "source_filing": "0000000000-26-000001",
    })


def test_severity_map_empty_without_data(fresh_db):
    assert db.reserving_severity_map() == {}


def test_tone_alone_enters_severity_map_capped(fresh_db):
    _adverse_disclosure(score=1.0)
    sev = db.reserving_severity_map()
    assert sev["PGR"] == pytest.approx(LANG_SEVERITY_CAP)   # ≤ 0.15, tone-only


def test_chain_ladder_dominates_when_larger(fresh_db):
    # Confirmed adverse triangle (0.30) outranks the tone-derived severity (≤0.15).
    db.upsert_reserving_signal({
        "insurer": "PGR", "lob": "auto", "metric": "incurred", "as_of": "2026-03-31",
        "ultimate": 600.0, "latest": 500.0, "ibnr": 100.0, "prior_ibnr": 80.0,
        "deterioration_pct": 0.30, "direction": "adverse",
    })
    _adverse_disclosure(score=1.0)
    assert db.reserving_severity_map()["PGR"] == pytest.approx(0.30)


def test_releasing_tone_does_not_enter_map(fresh_db):
    db.upsert_disclosure_sentiment({
        "insurer": "ALL", "period": "2026Q1", "as_of": "2026-02-15",
        "reserve_tone": "releasing", "adverse_language_score": 0.0,
        "source_filing": "x",
    })
    assert db.reserving_severity_map() == {}


# ── end-to-end run over a stored EDGAR item ──────────────────────────────────

def test_run_disclosure_scores_stored_edgar_filing(fresh_db, make_item):
    db.upsert_items([make_item(
        source="edgar", source_id="PGR:0001-26-1",
        title="PGR 8-K filed 2026-02-15",
        author="Progressive",
        content=_ADVERSE,
        published_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
        metadata={"ticker": "PGR", "form": "8-K", "accession": "0001-26-1"},
    )])
    counts = run_disclosure()
    assert counts == {"filings": 1, "scored": 1}
    rows = db.latest_disclosure_sentiment()
    assert len(rows) == 1
    assert rows[0]["insurer"] == "PGR"
    assert rows[0]["period"] == "2026Q1"
    assert rows[0]["reserve_tone"] == "strengthening"
    # The reading now feeds the boost severity map.
    assert db.reserving_severity_map().get("PGR", 0.0) > 0
