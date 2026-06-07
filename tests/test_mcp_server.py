"""Tests for the FastMCP data-analyst server (src/digest/mcp_server.py).

Skipped wholesale unless the optional `mcp` extra is installed (the module
imports FastMCP at top). Each test runs against the hermetic `fresh_db` temp
warehouse; the server reads settings.db_path at call time, so monkeypatching
the shared settings singleton (what fresh_db does) reaches the server too.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")  # FastMCP server module needs the `mcp` extra

from digest import db  # noqa: E402
from digest import mcp_server as m  # noqa: E402


def _seed_scored_item(make_item) -> int:
    """Insert one kept+summarized EDGAR item with a known signal_scores row."""
    db.upsert_items([make_item(source="edgar", source_id="PGR:1", title="Progressive 8-K")])
    with db.get_conn() as conn:
        (iid,) = conn.execute("SELECT id FROM items WHERE source_id = 'PGR:1'").fetchone()
    db.update_triage(iid, "keep", 0.95, "underwriting_results")
    db.update_summary(iid, "underwriting_results", "body", "why", "high", [])
    db.upsert_signal_scores([{
        "item_id": iid, "computed_at": "2026-01-01T00:00:00+00:00",
        "score": round(1.3 * 0.5 * 1.5, 4),  # source × recency × materiality
        "source_mult": 1.3, "regime_mult": 1.0, "topic_relevance": 1.0,
        "recency": 0.5, "llm_judgment": 1.5, "topic_boost": 1.0,
        "burden_boost": 1.0, "insurer_boost": 1.0, "inflation_boost": 1.0,
        "regulatory_boost": 1.0, "tplf_boost": 1.0, "tier": "medium",
    }])
    return iid


def test_list_and_describe_tables(fresh_db, make_item):
    db.upsert_items([make_item()])
    tables = {t["table"] for t in json.loads(m.list_tables())}
    assert {"items", "signal_scores", "run_log"} <= tables

    desc = json.loads(m.describe_table("items"))
    colnames = {c["name"] for c in desc["columns"]}
    assert {"id", "source", "topic", "triage_decision"} <= colnames

    with pytest.raises(ValueError):
        m.describe_table("no_such_table")


def test_run_sql_executes_and_caps(fresh_db, make_item):
    db.upsert_items([make_item(source_id=f"a{i}") for i in range(5)])
    out = json.loads(m.run_sql("SELECT id FROM items ORDER BY id", limit=2))
    assert out["columns"] == ["id"]
    assert out["row_count"] == 2
    assert out["truncated"] is True

    # Parameter binding works.
    one = json.loads(m.run_sql("SELECT COUNT(*) c FROM items WHERE source = ?", params=["rss"]))
    assert one["rows"][0]["c"] == 5


@pytest.mark.parametrize("bad", ["DELETE FROM items", "UPDATE items SET title='x'",
                                 "INSERT INTO items DEFAULT VALUES",
                                 "SELECT 1; SELECT 2"])
def test_run_sql_guard_rejects_writes_and_multistatement(fresh_db, bad):
    with pytest.raises(ValueError):
        m.run_sql(bad)


def test_run_sql_engine_is_read_only(fresh_db, make_item):
    """Even if the keyword guard were bypassed, mode=ro blocks writes at the engine."""
    db.upsert_items([make_item()])
    conn = m._ro_conn()
    try:
        with pytest.raises(Exception):  # sqlite3.OperationalError: readonly database
            conn.execute("DELETE FROM items")
    finally:
        conn.close()


def test_data_overview(fresh_db, make_item):
    _seed_scored_item(make_item)
    ov = json.loads(m.data_overview())
    assert ov["total_items"] == 1
    assert ov["items_scored"] == 1
    assert {"keep"} <= {f["decision"] for f in ov["triage_funnel"]}


def test_top_signals_and_score_breakdown(fresh_db, make_item):
    iid = _seed_scored_item(make_item)

    top = json.loads(m.top_signals(limit=5))
    assert top["items"] and top["items"][0]["id"] == iid

    bd = json.loads(m.score_breakdown(iid))
    # Reconstructed product must equal the stored score (factor list complete + ordered).
    assert bd["reconstructed_product"] == pytest.approx(bd["score"], abs=1e-4)
    lifting = {f["factor"] for f in bd["lifting_factors"]}
    dragging = {f["factor"] for f in bd["dragging_factors"]}
    assert {"source_mult", "llm_judgment"} <= lifting   # 1.3, 1.5
    assert "recency" in dragging                         # 0.5


def test_score_breakdown_unscored_item(fresh_db, make_item):
    db.upsert_items([make_item(source_id="unscored")])
    with db.get_conn() as conn:
        (iid,) = conn.execute("SELECT id FROM items WHERE source_id = 'unscored'").fetchone()
    bd = json.loads(m.score_breakdown(iid))
    assert bd["score"] is None
    assert "not yet scored" in bd["note"]

    with pytest.raises(ValueError):
        m.score_breakdown(999999)


def test_pipeline_health(fresh_db, make_item):
    db.upsert_items([make_item()])
    db.log_run("manual", "rss", 1, 1, 10, "ok")
    db.log_run("manual", "edgar", 0, 0, 5, "error", error="boom")
    ph = json.loads(m.pipeline_health(hours=72))
    sources = {r["source"] for r in ph["latest_run_per_source"]}
    assert {"rss", "edgar"} <= sources
    assert any(r["source"] == "edgar" for r in ph["error_runs"])
