"""Historical backfill — as-of correctness, provenance, and live-mix gating.

The backfill's whole value is honest labels, so these tests pin the three
disciplines: (1) recency/regime are computed as-of the filing date, never now;
(2) backfilled rows are provenance-tagged, enter at their historical
ingested_at, and stay out of the live scoring path; (3) a backfill-heavy label
set cannot pass the pooled log-linear gate on its own.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from digest import db
from digest.backfill import (
    regime_at,
    score_backfilled,
    select_historical_filings,
)
from digest.regime import RegimeSignal
from digest.signals import _recency, score_item


def _regime(mult: float = 1.0) -> RegimeSignal:
    return RegimeSignal(
        as_of="2024-09-01T00:00:00+00:00",
        market_cycle="stable", cat_load="low_season",
        market_cycle_mult=mult, cat_load_mult=1.0, multiplier=mult,
    )


# ── (1) as-of scoring ──────────────────────────────────────────────────


def test_recency_decays_from_as_of_not_now():
    published = "2024-09-01T16:00:00+00:00"
    # As-of one day after publication: a fresh item (~0.9 at 7d half-life)...
    fresh = _recency(published, None, topic="underwriting_results",
                     as_of=datetime(2024, 9, 2, 16, tzinfo=timezone.utc))
    assert fresh == pytest.approx(2 ** (-1 / 7), abs=0.01)
    # ...whereas the wall clock (years later) would floor it.
    stale = _recency(published, None, topic="underwriting_results")
    assert stale == 0.1
    assert fresh > stale


def test_score_item_threads_as_of():
    row = {
        "id": 1, "source": "edgar", "topic": "underwriting_results",
        "published_at": "2024-09-01T16:00:00+00:00",
        "ingested_at": "2024-09-02T08:00:00+00:00",
        "title": "PGR 8-K filed 2024-09-01", "summary": None,
        "materiality_score": None, "burden_intensity": None,
        "metadata_json": None, "sub_tags": None, "why_it_matters": None,
    }
    as_of = datetime(2024, 9, 2, 16, tzinfo=timezone.utc)
    s = score_item(row, _regime(), as_of=as_of)
    assert s.recency == pytest.approx(2 ** (-1 / 7), abs=0.01)


def test_regime_at_uses_history_and_falls_back_neutral(fresh_db):
    # No regime history → neutral 1.0, never today's multiplier.
    neutral = regime_at("2024-09-01T00:00:00")
    assert neutral.multiplier == 1.0
    assert neutral.source == "backfill_neutral"

    db.upsert_regime_signal(
        as_of="2025-01-01T00:00:00+00:00",
        market_cycle="hard_market", cat_load="low_season",
        market_cycle_mult=1.2, cat_load_mult=1.0, multiplier=1.2,
        evidence_json="{}",
    )
    # Before the first regime row → still neutral; after → the stored row.
    assert regime_at("2024-12-31T00:00:00").multiplier == 1.0
    assert regime_at("2025-02-01T00:00:00").multiplier == 1.2


# ── (2) provenance + historical ingested_at + live-path exclusion ──────


def _backfill_item(make_item, source_id: str, filed: datetime, **kw):
    return make_item(
        source="edgar", source_id=source_id,
        title=f"PGR 8-K filed {filed:%Y-%m-%d}",
        published_at=filed,
        metadata={"ticker": "PGR", "form": "8-K", "backfill": True},
        **kw,
    )


def test_insert_backfill_items_sets_historical_ingested_at(fresh_db, make_item):
    filed = datetime(2024, 9, 1, tzinfo=timezone.utc)
    n = db.insert_backfill_items([_backfill_item(make_item, "PGR:0001", filed)])
    assert n == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT ingested_at, metadata_json FROM items WHERE source_id='PGR:0001'"
        ).fetchone()
    assert row["ingested_at"].startswith("2024-09-01T16:00")
    assert '"backfill": true' in row["metadata_json"]


def test_insert_backfill_items_never_clobbers_live_rows(fresh_db, make_item):
    live = make_item(source="edgar", source_id="PGR:0002", title="live",
                     metadata={"ticker": "PGR", "form": "8-K"})
    db.upsert_items([live])
    filed = datetime(2024, 9, 1, tzinfo=timezone.utc)
    n = db.insert_backfill_items([_backfill_item(make_item, "PGR:0002", filed)])
    assert n == 0
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title, metadata_json FROM items WHERE source_id='PGR:0002'"
        ).fetchone()
    assert row["title"] == "live"
    assert "backfill" not in row["metadata_json"]


def test_insert_backfill_items_skips_missing_published_at(fresh_db, make_item):
    item = make_item(source="edgar", source_id="PGR:0003",
                     metadata={"backfill": True})  # published_at=None
    assert db.insert_backfill_items([item]) == 0


def test_live_scoring_excludes_backfill_and_backfill_queue_selects_it(fresh_db, make_item):
    filed = datetime(2024, 9, 1, tzinfo=timezone.utc)
    db.insert_backfill_items([_backfill_item(make_item, "PGR:0004", filed)])
    db.upsert_items([make_item(source="rss", source_id="live-1", title="live item")])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep', summary='s', topic='personal_lines'")

    live_ids = {r["source"] for r in db.items_for_signals()}
    assert live_ids == {"rss"}

    queue = db.backfill_items_for_signals()
    assert [r["source"] for r in queue] == ["edgar"]

    # Once scored, the backfill queue drains (idempotent re-runs).
    summary = score_backfilled()
    assert summary["scored"] == 1
    assert db.backfill_items_for_signals() == []


def test_score_backfilled_persists_as_of_score(fresh_db, make_item):
    filed = datetime(2024, 9, 1, tzinfo=timezone.utc)
    db.insert_backfill_items([_backfill_item(make_item, "PGR:0005", filed)])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep', summary='s', "
                     "topic='underwriting_results', materiality_score=1.0")
    assert score_backfilled()["scored"] == 1
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT s.computed_at, s.recency, s.regime_mult FROM signal_scores s
               JOIN items i ON i.id = s.item_id WHERE i.source_id='PGR:0005'"""
        ).fetchone()
    # computed_at is the historical as-of (ingested + 24h), not now…
    assert row["computed_at"].startswith("2024-09-02")
    # …recency reflects ~1d 8h of age, and regime is neutral (no history).
    assert row["recency"] > 0.8
    assert row["regime_mult"] == 1.0


# ── (3) live-mix gate + selection purity ───────────────────────────────


def test_loglinear_gate_blocks_backfill_only_labels(monkeypatch):
    from digest import loglinear

    def _rows(is_backfill: int):
        rows = []
        for i in range(400):
            rows.append({
                "item_id": i, "ingested_at": f"2024-{1 + i % 12:02d}-01T00:00:00",
                "is_backfill": is_backfill, "corroborated": i % 2,
                "materiality_score": 1.0,
                **{f: 1.0 + (i % 5) * 0.1 for f in loglinear.FACTORS},
            })
        return rows

    monkeypatch.setattr(loglinear.db, "learning_dataset", lambda h: _rows(1))
    monkeypatch.setattr(loglinear.db, "save_loglinear_eval", lambda d: 1)
    monkeypatch.setattr(loglinear, "is_eligible", lambda: False)
    out = loglinear.evaluate()
    assert out["passed"] is False
    assert out["n_live"] == 0
    assert "live-mix gate" in out["note"]

    # Same data flagged live: the live-mix gate no longer blocks (the verdict
    # then rests on the AUC criteria alone, and no note is attached).
    monkeypatch.setattr(loglinear.db, "learning_dataset", lambda h: _rows(0))
    out = loglinear.evaluate()
    assert out["n_live"] == 400
    assert "note" not in out or "live-mix" not in out.get("note", "")


def test_select_historical_filings_filters_form_and_date():
    pages = [
        {   # recent-shaped parallel arrays
            "form": ["8-K", "4", "10-Q", "8-K"],
            "filingDate": ["2024-09-03", "2024-09-02", "2024-08-01", "2023-01-01"],
            "accessionNumber": ["a1", "a2", "a3", "a4"],
            "primaryDocument": ["d1", "d2", "d3", "d4"],
        },
        {   # older archive page
            "form": ["10-K"],
            "filingDate": ["2024-02-25"],
            "accessionNumber": ["a5"],
            "primaryDocument": ["d5"],
        },
    ]
    out = select_historical_filings(pages, since="2024-01-01")
    assert [f["accession"] for f in out] == ["a5", "a3", "a1"]   # chronological
    forms = {f["form"] for f in out}
    assert "4" not in forms          # irrelevant form dropped
    assert all(f["filing_date"] >= "2024-01-01" for f in out)


def test_learning_dataset_reports_backfill_flag(fresh_db, make_item):
    filed = datetime(2024, 9, 1, tzinfo=timezone.utc)
    db.insert_backfill_items([_backfill_item(make_item, "PGR:0006", filed)])
    db.upsert_items([make_item(source="rss", source_id="live-2", title="live")])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep', summary='s', topic='personal_lines'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM items ORDER BY id")]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.upsert_signal_scores([
        {"item_id": i, "computed_at": now_iso, "score": 1.0, "tier": "medium",
         "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0,
         "recency": 1.0, "llm_judgment": 1.0, "topic_boost": 1.0,
         "burden_boost": 1.0, "insurer_boost": 1.0, "inflation_boost": 1.0,
         "regulatory_boost": 1.0, "tplf_boost": 1.0, "reserve_boost": 1.0,
         "learned_score": None}
        for i in ids
    ])
    for i in ids:
        db.upsert_backtest_outcome(i, 7, {"corroborated": True, "signals": ["followon"]})
    rows = db.learning_dataset(7)
    flags = {r["item_id"]: r["is_backfill"] for r in rows}
    assert sorted(flags.values()) == [0, 1]
