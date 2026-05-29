"""Semantic layer (Databricks Option 3) — embeddings + local kNN + RAG.

Free-Edition design: embeddings are computed on the Mac mini via the already-
running Ollama server (no torch dependency), cached in SQLite (item_embeddings)
and mirrored to pc_bronze.item_embeddings. Retrieval is a brute-force numpy
cosine over the (few-thousand-row) corpus — instant at this scale, and the
clean upgrade path is a native Databricks Vector Search Delta Sync Index +
vector_search() once off Free Edition.

Powers three things:
  - related(item_id)        — "more like this" cross-links / see-also
  - near_duplicates(...)    — semantic dedup (sharper than title fuzzing)
  - ask(question)           — RAG: retrieve top-k items, answer via an LLM backend

The Ollama transport (`embed_texts`) is the only non-pure part; the kNN math and
the ask prompt assembly are pure/injectable so they test without a live server.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import requests

from digest import db
from digest.config import settings
from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError

logger = logging.getLogger(__name__)


# ── Embedding transport (Ollama) ─────────────────────────────────────────


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed each text via Ollama /api/embeddings. Raises on an unreachable server.

    One request per text (Ollama's classic endpoint); fine for the modest batch
    sizes here. Empty/whitespace text embeds its title-less placeholder so every
    item still gets a vector.
    """
    url = settings.ollama_host.rstrip("/") + "/api/embeddings"
    out: list[list[float]] = []
    for text in texts:
        payload = {"model": settings.embedding_model, "prompt": text or "(no content)"}
        try:
            r = requests.post(url, json=payload, timeout=60)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise BackendError(
                f"Ollama embeddings unreachable at {url} "
                f"(model={settings.embedding_model!r}): {exc}. "
                "Start Ollama and `ollama pull nomic-embed-text`."
            ) from exc
        vec = r.json().get("embedding")
        if not vec:
            raise BackendError(f"Ollama returned no embedding for model {settings.embedding_model!r}")
        out.append(vec)
    return out


def _item_text(title: str | None, summary: str | None) -> str:
    """The text we embed per item — title carries the strongest signal, summary
    adds context."""
    parts = [p for p in (title, summary) if p]
    return "\n".join(parts) if parts else "(no content)"


# ── Pure kNN ──────────────────────────────────────────────────────────────


def _matrix(rows: list) -> tuple[list[int], np.ndarray]:
    ids = [r["item_id"] for r in rows]
    mat = np.array([json.loads(r["vector_json"]) for r in rows], dtype=float)
    return ids, mat


def cosine_topk(qvec, mat: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Top-k (row_index, cosine_similarity) of `qvec` against rows of `mat`."""
    if mat.size == 0:
        return []
    q = np.asarray(qvec, dtype=float)
    qn = q / (np.linalg.norm(q) or 1.0)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = mn @ qn
    order = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in order]


# ── Public operations ───────────────────────────────────────────────────


def run_embed(limit: int = 500) -> dict[str, int]:
    """Embed kept items that lack a vector. Returns {needed, embedded}."""
    rows = db.items_needing_embedding(limit=limit)
    if not rows:
        return {"needed": 0, "embedded": 0}
    vectors = embed_texts([_item_text(r["title"], r["summary"]) for r in rows])
    n = 0
    for r, vec in zip(rows, vectors):
        db.upsert_embedding(r["id"], settings.embedding_model, vec)
        n += 1
    logger.info("semantic: embedded %d/%d items", n, len(rows))
    return {"needed": len(rows), "embedded": n}


def related(item_id: int, k: int = 5) -> list[dict]:
    """Items most similar to `item_id` (excludes itself). [] if it's unembedded."""
    rows = db.load_embeddings()
    if not rows:
        return []
    ids, mat = _matrix(rows)
    if item_id not in ids:
        return []
    qvec = mat[ids.index(item_id)]
    results = []
    for idx, sim in cosine_topk(qvec, mat, k + 1):
        if ids[idx] == item_id:
            continue
        r = rows[idx]
        results.append({
            "item_id": ids[idx], "score": sim,
            "title": r["title"], "topic": r["topic"], "source": r["source"], "url": r["url"],
        })
    return results[:k]


def near_duplicates(item_id: int, threshold: float = 0.92, k: int = 10) -> list[dict]:
    """Items whose cosine similarity to `item_id` meets `threshold` — semantic dedup."""
    return [r for r in related(item_id, k=k) if r["score"] >= threshold]


_ASK_SYSTEM_PROMPT = (
    "You are a research analyst for a US P&C insurance and financial-services "
    "digest. Answer the user's question using ONLY the numbered digest items "
    "provided as context. Cite the item numbers you rely on like [#3]. If the "
    "context does not contain the answer, say so plainly — do not invent facts."
)


def ask(question: str, k: int = 8, backend_name: str | None = None) -> dict:
    """RAG over the corpus: embed the question, retrieve top-k items, answer via
    an LLM backend. Returns {answer, sources}. `sources` is always returned even
    if the LLM call fails, so retrieval is useful on its own."""
    rows = db.load_embeddings()
    if not rows:
        return {"answer": "", "sources": [], "error": "no embeddings yet — run `digest embed`"}
    ids, mat = _matrix(rows)
    qvec = embed_texts([question])[0]
    hits = cosine_topk(qvec, mat, k)
    hit_ids = [ids[idx] for idx, _ in hits]
    texts = db.get_items_text(hit_ids)

    sources, context_blocks = [], []
    for n, (idx, sim) in enumerate(hits, start=1):
        iid = ids[idx]
        it = texts.get(iid)
        if not it:
            continue
        sources.append({"n": n, "item_id": iid, "score": sim,
                        "title": it["title"], "source": it["source"], "url": it["url"]})
        context_blocks.append(
            f"[#{n}] ({it['source']} · {it['topic'] or '?'}) {it['title']}\n"
            f"{(it['summary'] or '').strip()}\n"
            f"Why it matters: {(it['why_it_matters'] or '').strip()}"
        )

    user_prompt = (
        f"Question: {question}\n\n"
        f"Context — top {len(context_blocks)} digest items:\n\n"
        + "\n\n".join(context_blocks)
        + "\n\nAnswer (cite item numbers):"
    )
    backend_name = backend_name or settings.summarizer_backend
    backend_fn = BACKENDS.get(backend_name)
    if backend_fn is None:
        return {"answer": "", "sources": sources, "error": f"unknown backend {backend_name!r}"}
    cfg = BackendConfig(
        timeout_sec=settings.summarizer_timeout_sec,
        claude_model=settings.summarizer_model,
        anthropic_api_key=settings.anthropic_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_host=settings.ollama_host,
        ollama_model=settings.ollama_model,
        mlx_server_url=settings.mlx_server_url,
        mlx_model=settings.mlx_model,
    )
    try:
        answer = backend_fn(_ASK_SYSTEM_PROMPT, user_prompt, cfg)
    except BackendError as exc:
        return {"answer": "", "sources": sources, "error": str(exc)}
    return {"answer": answer.strip(), "sources": sources, "error": None}
