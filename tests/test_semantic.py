"""Option 3 — semantic layer: kNN math, related/dedup, run_embed, ask (RAG).

Hermetic: no live Ollama/LLM. embed_texts + the LLM backend are monkeypatched;
vectors are injected via db.upsert_embedding (sink off under fresh_db).
"""
from __future__ import annotations

import numpy as np

from digest import db, semantic


# ── pure kNN ─────────────────────────────────────────────────────────────


def test_cosine_topk_orders_by_similarity():
    mat = np.array([[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]])
    hits = semantic.cosine_topk([1.0, 0.0], mat, k=3)
    assert hits[0][0] == 0 and hits[0][1] > 0.99      # identical direction
    assert hits[1][0] == 2                             # near-parallel next
    assert hits[2][0] == 1                             # orthogonal last


def test_cosine_topk_empty_matrix():
    assert semantic.cosine_topk([1.0, 0.0], np.empty((0, 2)), k=3) == []


# ── related / near_duplicates (injected vectors) ─────────────────────────


def _seed(make_item, vectors: dict[str, list[float]]):
    db.upsert_items([make_item(source="rss", source_id=sid, title=f"item {sid}")
                     for sid in vectors])
    with db.get_conn() as conn:
        ids = {r["source_id"]: r["id"]
               for r in conn.execute("SELECT id, source_id FROM items").fetchall()}
    for sid, vec in vectors.items():
        db.upsert_embedding(ids[sid], "test-model", vec)
    return ids


def test_related_ranks_nearest_excluding_self(fresh_db, make_item):
    ids = _seed(make_item, {
        "a": [1.0, 0.0, 0.0],
        "b": [0.95, 0.05, 0.0],   # very close to a
        "c": [0.0, 1.0, 0.0],     # orthogonal
    })
    hits = semantic.related(ids["a"], k=5)
    assert ids["a"] not in [h["item_id"] for h in hits]   # excludes self
    assert hits[0]["item_id"] == ids["b"]                  # nearest first
    assert hits[0]["score"] > hits[-1]["score"]


def test_near_duplicates_threshold(fresh_db, make_item):
    ids = _seed(make_item, {
        "a": [1.0, 0.0],
        "dup": [0.999, 0.001],   # ~identical
        "far": [0.2, 0.98],
    })
    dups = semantic.near_duplicates(ids["a"], threshold=0.92)
    dup_ids = [d["item_id"] for d in dups]
    assert ids["dup"] in dup_ids and ids["far"] not in dup_ids


def test_related_unembedded_item_returns_empty(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="x")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id='x'").fetchone()["id"]
    assert semantic.related(iid) == []


# ── run_embed (mock the transport) ───────────────────────────────────────


def test_run_embed_persists(fresh_db, make_item, monkeypatch):
    db.upsert_items([make_item(source="rss", source_id="k1", title="Kept")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id='k1'").fetchone()["id"]
        conn.execute("UPDATE items SET triage_decision='keep' WHERE id=?", (iid,))

    monkeypatch.setattr(semantic, "embed_texts", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
    counts = semantic.run_embed()
    assert counts == {"needed": 1, "embedded": 1}
    assert len(db.load_embeddings()) == 1
    # idempotent: nothing left needing an embedding
    assert semantic.run_embed()["needed"] == 0


# ── ask (mock transport + backend) ───────────────────────────────────────


def test_ask_retrieves_and_answers(fresh_db, make_item, monkeypatch):
    db.upsert_items([
        make_item(source="rss", source_id="fl", title="Florida FAIR Plan grows"),
        make_item(source="rss", source_id="ca", title="California wildfire exits"),
    ])
    with db.get_conn() as conn:
        ids = {r["source_id"]: r["id"]
               for r in conn.execute("SELECT id, source_id FROM items").fetchall()}
        for sid in ids:
            conn.execute(
                "UPDATE items SET triage_decision='keep', summary=? WHERE id=?",
                (f"summary for {sid}", ids[sid]))
    db.upsert_embedding(ids["fl"], "m", [1.0, 0.0])
    db.upsert_embedding(ids["ca"], "m", [0.0, 1.0])

    # query embeds parallel to the FL item; stub the LLM backend.
    monkeypatch.setattr(semantic, "embed_texts", lambda texts: [[1.0, 0.05]])
    monkeypatch.setitem(semantic.BACKENDS, "fake", lambda sys, usr, cfg: "FAIR Plan exposure is rising [#1].")

    result = semantic.ask("What about FL FAIR Plan?", k=2, backend_name="fake")
    assert result["error"] is None
    assert "FAIR Plan" in result["answer"]
    assert result["sources"][0]["item_id"] == ids["fl"]   # FL ranked first


def test_ask_without_embeddings_is_graceful(fresh_db):
    result = semantic.ask("anything", backend_name="fake")
    assert result["answer"] == "" and "no embeddings" in result["error"]
