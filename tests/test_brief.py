"""Option 2 — the local `digest brief` alert watchlist (brief_alerts)."""
from __future__ import annotations

from digest import db


def _keep(conn, item_id, **cols):
    extra = "".join(f", {k} = ?" for k in cols)   # leading comma; empty when no cols
    conn.execute(
        f"UPDATE items SET triage_decision='keep', triaged_at=datetime('now'){extra} WHERE id=?",
        (*cols.values(), item_id),
    )


def test_brief_alerts_buckets(fresh_db, make_item):
    db.upsert_items([
        make_item(source="serff", source_id="b1", title="CA rate suppression bill"),
        make_item(source="courtlistener", source_id="t1", title="$90M nuclear verdict"),
        make_item(source="rss", source_id="n1", title="Routine filing"),
        make_item(source="fred", source_id="f1", title="Body-shop labor PPI +2.1σ"),
    ])
    with db.get_conn() as conn:
        ids = {r["source_id"]: r["id"] for r in
               conn.execute("SELECT id, source_id FROM items").fetchall()}
        _keep(conn, ids["b1"], burden_intensity="high", burden_direction="increasing")
        _keep(conn, ids["t1"], sub_tags='["litigation_tplf"]')
        _keep(conn, ids["n1"])  # plain keep — should not alert

    a = db.brief_alerts(hours=48)
    assert [r["title"] for r in a["high_burden"]] == ["CA rate suppression bill"]
    assert [r["title"] for r in a["tplf"]] == ["$90M nuclear verdict"]
    assert [r["title"] for r in a["fred"]] == ["Body-shop labor PPI +2.1σ"]
    assert a["degraded"] == []


def test_brief_alerts_flags_degraded_source(fresh_db):
    db.log_run(run_type="manual", source="nhc", items_fetched=0, items_new=0,
               duration_ms=5, status="error", error="HTTP 503 from NHC")
    db.log_run(run_type="manual", source="rss", items_fetched=10, items_new=3,
               duration_ms=5, status="ok")
    a = db.brief_alerts()
    degraded = {r["source"]: r["error"] for r in a["degraded"]}
    assert "nhc" in degraded and "503" in degraded["nhc"]
    assert "rss" not in degraded


def test_brief_alerts_window_excludes_old(fresh_db, make_item):
    db.upsert_items([make_item(source="serff", source_id="old1", title="Old burden item")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id='old1'").fetchone()["id"]
        conn.execute(
            """UPDATE items SET triage_decision='keep', burden_intensity='high',
               triaged_at=datetime('now','-5 days') WHERE id=?""", (iid,))
    assert db.brief_alerts(hours=48)["high_burden"] == []
