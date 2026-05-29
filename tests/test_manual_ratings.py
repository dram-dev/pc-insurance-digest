"""Calibration loop (Databricks Option 1a) — manual_ratings activation.

The user's manual rating is the input behind gold.score_calibration. These
cover the SQLite persistence + sink fan-out (hermetic: sink forced off) and the
core sink's write_rating row shaping.
"""
from __future__ import annotations

from digest import db
from digest_core.sinks.databricks import DatabricksSink, item_hash


# ── SQLite persistence + history ─────────────────────────────────────────


def test_manual_ratings_table_exists(fresh_db):
    with db.get_conn() as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "manual_ratings" in tables


def test_upsert_manual_rating_persists(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="r1", title="FAIR Plan move")])
    with db.get_conn() as conn:
        item_id = conn.execute("SELECT id FROM items WHERE source_id='r1'").fetchone()["id"]

    db.upsert_manual_rating(item_id, 5.0, note="exactly the signal I want")
    rows = db.recent_manual_ratings()
    assert len(rows) == 1
    assert rows[0]["user_rating"] == 5.0
    assert rows[0]["note"] == "exactly the signal I want"
    assert rows[0]["title"] == "FAIR Plan move"


def test_rerating_keeps_history(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="r1")])
    with db.get_conn() as conn:
        item_id = conn.execute("SELECT id FROM items WHERE source_id='r1'").fetchone()["id"]

    db.upsert_manual_rating(item_id, 2.0, rated_at="2026-05-28T10:00:00")
    db.upsert_manual_rating(item_id, 4.0, rated_at="2026-05-29T10:00:00")
    rows = db.recent_manual_ratings()
    assert len(rows) == 2                       # distinct rated_at → history kept
    assert rows[0]["user_rating"] == 4.0        # newest first


def test_upsert_manual_rating_unknown_item_is_safe(fresh_db):
    # No item row → no sink fan-out, no crash (SQLite row still written).
    db.upsert_manual_rating(99999, 3.0)
    rows = db.recent_manual_ratings()
    assert rows == []                           # join to items finds nothing


def test_calibration_rows_joins_latest_score(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="scored", title="Scored item"),
                     make_item(source="rss", source_id="unscored", title="Unscored item")])
    with db.get_conn() as conn:
        ids = {r["source_id"]: r["id"] for r in
               conn.execute("SELECT id, source_id FROM items").fetchall()}

    # Give one item a leaderboard score; rate both.
    db.upsert_signal_scores([{
        "item_id": ids["scored"], "computed_at": "2026-05-29T09:00:00", "score": 2.4,
        "source_mult": 1.0, "regime_mult": 1.0, "topic_relevance": 1.0, "recency": 1.0,
        "llm_judgment": 1.0, "topic_boost": 1.0, "burden_boost": 1.0,
        "insurer_boost": 1.0, "inflation_boost": 1.0, "regulatory_boost": 1.0,
        "tplf_boost": 1.0, "tier": "high",
    }])
    db.upsert_manual_rating(ids["scored"], 5.0)
    db.upsert_manual_rating(ids["unscored"], 3.0)

    by_item = {r["item_id"]: r for r in db.calibration_rows()}
    assert by_item[ids["scored"]]["system_score"] == 2.4
    assert by_item[ids["scored"]]["user_rating"] == 5.0
    assert by_item[ids["unscored"]]["system_score"] is None   # rated, never scored


# ── core sink row shaping (no live connection) ───────────────────────────


def test_write_rating_noop_when_disabled():
    # Disabled sink must never attempt a connection.
    sink = DatabricksSink(enabled=False, host="", http_path="", token="", catalog="")
    sink.write_rating("rss", "r1", {"user_rating": 5.0, "note": "x"})  # no raise


def test_write_rating_builds_item_hash(monkeypatch):
    sink = DatabricksSink(enabled=True, host="h", http_path="p", token="t", catalog="c")
    captured = {}

    def fake_insert(table, rows):
        captured["table"] = table
        captured["rows"] = rows

    monkeypatch.setattr(sink, "_insert", fake_insert)
    sink.write_rating("rss", "r1", {"user_rating": 4.0, "note": "n",
                                    "rated_at": "2026-05-29T10:00:00"})
    assert captured["table"] == "silver.manual_ratings"
    row = captured["rows"][0]
    assert row["item_hash"] == item_hash("rss", "r1")
    assert row["user_rating"] == 4.0
    assert row["rated_at"] == "2026-05-29T10:00:00"
