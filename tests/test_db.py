"""Regression tests for the SQLite layer after the digest-core db lift.

`src/digest/db.py` now delegates schema + generic CRUD to `digest_core.db`
while keeping the PC-domain migrations, auto-keep hooks, and the sink
fan-out. These tests lock that contract: the base schema + PC migrations
land, the generic helpers round-trip through the thin wrappers, and the
PC auto-keep hooks (which compose on the rewired `get_conn`) still behave.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from digest import db


# ── schema ────────────────────────────────────────────────────────────


def test_init_db_is_idempotent(fresh_db):
    db.init_db(fresh_db)  # second apply must not raise
    db.init_db(fresh_db)


def test_base_and_migration_tables_exist(fresh_db):
    with db.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    # base schema owned by digest_core
    assert {"items", "run_log", "summarizer_log"} <= tables
    # PC-domain migrations layered on top
    assert {
        "fred_baseline", "regime_signals", "signal_scores", "signal_outcomes",
        "daily_connections", "macro_regime", "upcoming_events",
    } <= tables


def test_items_migration_columns_present(fresh_db):
    with db.get_conn() as conn:
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        score_cols = {r[1] for r in conn.execute("PRAGMA table_info(signal_scores)")}
    assert {
        "sub_tags", "burden_direction", "burden_intensity",
        "materiality_score", "obsidian_written_at", "cluster_id",
    } <= item_cols
    assert {
        "tplf_boost", "insurer_boost", "inflation_boost", "regulatory_boost",
    } <= score_cols


# ── generic helpers (delegated to digest_core) ──────────────────────────


def test_upsert_items_dedups(fresh_db, make_item):
    items = [make_item(source_id="x1")]
    assert db.upsert_items(items) == 1          # new
    assert db.upsert_items(items) == 0          # UNIQUE(source, source_id) ignored
    assert db.upsert_items([]) == 0             # empty short-circuit


def test_upsert_serializes_metadata_and_published_at(fresh_db, make_item):
    dt = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    db.upsert_items([make_item(source_id="m1", published_at=dt, metadata={"ticker": "PGR"})])
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT published_at, metadata_json FROM items WHERE source_id='m1'"
        ).fetchone()
    assert row["published_at"] == dt.isoformat()
    assert json.loads(row["metadata_json"]) == {"ticker": "PGR"}


def test_log_run_and_item_stats(fresh_db, make_item):
    db.upsert_items([
        make_item(source="rss", source_id="r1"),
        make_item(source="edgar", source_id="e1"),
    ])
    db.log_run("manual", "rss", 5, 1, 100, "ok")
    assert db.item_stats() == {"rss": 1, "edgar": 1}
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 1


def test_recent_items_source_filter(fresh_db, make_item):
    db.upsert_items([
        make_item(source="rss", source_id="r1"),
        make_item(source="hn", source_id="h1"),
    ])
    assert len(db.recent_items()) == 2
    rss = db.recent_items(source="rss")
    assert len(rss) == 1 and rss[0]["source"] == "rss"


def test_recent_kept_titles_only_returns_kept(fresh_db, make_item):
    db.upsert_items([
        make_item(source_id="k1", title="Keep me"),
        make_item(source_id="d1", title="Drop me"),
    ])
    ids = _ids_by_source_id(db)
    db.update_triage(ids["k1"], "keep", 0.9, "cyber")
    db.update_triage(ids["d1"], "drop", 0.1, None)
    assert db.recent_kept_titles() == ["Keep me"]


# ── pipeline contract: triage → summary → publish ───────────────────────


def test_triage_summary_publish_contract(fresh_db, make_item):
    db.upsert_items([
        make_item(source_id="s1", title="Summarized item"),
        make_item(source_id="u1", title="Kept but unsummarized"),
    ])
    ids = _ids_by_source_id(db)
    db.update_triage(ids["s1"], "keep", 0.9, "cyber")
    db.update_triage(ids["u1"], "keep", 0.8, "personal_lines")
    db.update_summary(ids["s1"], "cyber", "A summary.", "Why it matters.", "high", ["[[Other]]"])

    today = db.utcnow_iso()[:10]
    bucket = db.items_for_publish(today)
    assert "Summarized item" in [r["title"] for r in bucket["summarized"]]
    assert "Kept but unsummarized" in [r["title"] for r in bucket["kept_unsummarized"]]


# ── PC auto-keep hooks (domain code over the rewired get_conn) ───────────


def test_auto_keep_quantitative_uses_topic_hint(fresh_db, make_item):
    db.upsert_items([make_item(source="fred", source_id="f1", metadata={"topic_hint": "supply_chain"})])
    assert db.auto_keep_quantitative() == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT triage_decision, topic, triage_score FROM items WHERE source_id='f1'"
        ).fetchone()
    assert row["triage_decision"] == "keep"
    assert row["topic"] == "supply_chain"
    assert row["triage_score"] == 0.85


def test_auto_keep_insurer_filings_locks_topic_by_form(fresh_db, make_item):
    db.upsert_items([
        make_item(source="edgar", source_id="8k", metadata={"ticker": "PGR", "form": "8-K"}),
        make_item(source="edgar", source_id="13f", metadata={"ticker": "BRK", "form": "13F-HR"}),
        make_item(source="edgar", source_id="noise", metadata={"ticker": "ZZZ", "form": "8-K"}),
    ])
    assert db.auto_keep_insurer_filings({"PGR", "BRK"}, {"8-K", "13F-HR"}) == 2
    with db.get_conn() as conn:
        rows = {
            r["source_id"]: r
            for r in conn.execute("SELECT source_id, topic, triage_decision FROM items")
        }
    assert rows["8k"]["topic"] == "underwriting_results"
    assert rows["13f"]["topic"] == "ma_capital"
    assert rows["noise"]["triage_decision"] is None  # ticker not in universe → left for the LLM


# ── helpers ──────────────────────────────────────────────────────────────


def _ids_by_source_id(db_mod) -> dict[str, int]:
    with db_mod.get_conn() as conn:
        return {r["source_id"]: r["id"] for r in conn.execute("SELECT id, source_id FROM items")}
