"""Regulatory Burden Barometer (EKG Lead 9) — triage state extraction + roll-up.

Network-free: drives the triage verdict normalizer and the per-state reader; the
state column flows items.state ← triage ← _normalize_verdict.
"""
from __future__ import annotations

from digest import db, triage


def _verdict(topic, state=None, intensity=None, direction=None):
    return triage._normalize_verdict({
        "decision": "keep", "score": 0.8, "topic": topic,
        "burden_direction": direction, "burden_intensity": intensity, "state": state,
    })


def test_state_extracted_only_for_regulatory_rate():
    assert _verdict("regulatory_rate", state="CA")["state"] == "CA"
    assert _verdict("regulatory_rate", state="ca")["state"] == "CA"      # normalized upper
    # Non-regulatory topic → state dropped.
    assert _verdict("personal_lines", state="CA")["state"] is None


def test_invalid_or_multistate_state_is_null():
    assert _verdict("regulatory_rate", state="USA")["state"] is None     # not a 2-letter code
    assert _verdict("regulatory_rate", state="")["state"] is None
    assert _verdict("regulatory_rate", state=None)["state"] is None


def _keep_reg_item(make_item, sid, state, intensity, direction):
    db.upsert_items([make_item(source="rss", source_id=sid, title=f"{state} rate action")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id=?", (sid,)).fetchone()["id"]
    db.update_triage(
        item_id=iid, decision="keep", score=0.8, topic="regulatory_rate",
        burden_direction=direction, burden_intensity=intensity, state=state,
    )
    return iid


def test_update_triage_persists_state(fresh_db, make_item):
    iid = _keep_reg_item(make_item, "r1", "FL", "high", "increasing")
    with db.get_conn() as conn:
        row = conn.execute("SELECT state, topic FROM items WHERE id=?", (iid,)).fetchone()
    assert row["state"] == "FL"


def test_burden_by_state_intensity_weighted(fresh_db, make_item):
    _keep_reg_item(make_item, "r1", "FL", "high", "increasing")     # weight 3
    _keep_reg_item(make_item, "r2", "FL", "medium", "increasing")   # weight 2
    _keep_reg_item(make_item, "r3", "CA", "low", "decreasing")      # weight 1
    rows = db.burden_by_state()
    by_state = {r["state"]: r for r in rows}
    assert by_state["FL"]["weighted_burden"] == 5
    assert by_state["FL"]["n"] == 2
    assert by_state["FL"]["net_direction"] == 2                     # two increasing
    assert by_state["CA"]["weighted_burden"] == 1
    assert by_state["CA"]["net_direction"] == -1                    # one decreasing
    # FL (5) outranks CA (1).
    assert rows[0]["state"] == "FL"


def test_burden_by_state_empty_without_data(fresh_db):
    assert db.burden_by_state() == []
