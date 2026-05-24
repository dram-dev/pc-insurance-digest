"""Phase 2 — Summarize. Backend-abstracted, with a kill-switch.

Backends share a common interface: take an item dict, return a SummaryOutput.
Default backend is claude_cli_pro (uses your Claude Pro subscription via the
`claude -p` CLI). When Pro rate limits collide with interactive use, flip
SUMMARIZER_BACKEND in .env to switch — no code change.

Backends:
  - claude_cli_pro:    invokes `claude -p` headless on Sonnet. $0 (subscription).
  - haiku_api:         direct Anthropic API, Haiku 4.5 + caching. ~$0.50-1/mo.
  - gemini_flash_free: Google AI Studio free tier. $0.
  - local_qwen:        Ollama Qwen 14B. $0, lower polish on prose.

For all backends, summarizer_log records duration + char counts so you can
see actual usage vs. expectations in `digest stats --summarizer`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)


# ── Output schema ──────────────────────────────────────────────────────


@dataclass
class SummaryOutput:
    topic: str                       # canonical topic from triage taxonomy
    summary: str                     # 2-3 sentences
    why_it_matters: str              # 1-2 sentences, user-specific framing
    confidence: str                  # "low" | "medium" | "high"
    see_also: list[str] = field(default_factory=list)


# ── Prompt construction ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a personal research analyst. The reader spans four personas, served by one digest:
industry analyst, public insurer investor, P&C underwriter/risk professional, and broker / market-intelligence reader.
Focus is US P&C insurance and financial services: catastrophe events, reinsurance cycle, regulation, underwriting results,
reserves, M&A and capital, climate risk, cyber, social inflation, insurtech & AI, distribution, personal lines,
commercial/specialty, rates & cost of capital, supply chain, analytics & modeling.

For each item, produce a JSON object with five fields:

1. "topic": exactly one of [cat_event, reinsurance_cycle, regulatory_rate, underwriting_results, reserving, ma_capital,
   climate_risk, cyber, social_inflation, ai_insurtech, distribution, personal_lines, commercial_specialty,
   macro_linkage, rates_cost_of_capital, supply_chain, analytics_modeling]
2. "summary": 2-3 sentences. State the actual content — what was reported, filed, modeled, or claimed. No filler.
3. "why_it_matters": 1-2 sentences. Frame the implication for the P&C reader: P&L impact, capital, capacity, reserves,
   cycle direction, regulatory exposure, or distribution shift. Be specific. Bad: "important for insurers."
   Good: "Adverse development on 2022 GL accident year suggests TRV reserve guide may be light vs. consensus."
4. "confidence": "low" | "medium" | "high" — how reliable is the signal?
   High = primary source (insurer 10-K/8-K, NHC advisory, AM Best rating action, NAIC release, named industry expert).
   Medium = reputable secondary reporting (Insurance Journal, Reinsurance News, Artemis, WSJ/FT/Bloomberg insurance desk).
   Low = social-media speculation, anonymous posts, single-source claims.
5. "see_also": a list of 0-3 short phrases describing connected events, peers, or themes. Examples:
   "FL hurricane CAT load", "1/1 reinsurance renewal pricing", "MMC Q3 organic growth", "TPLF disclosure rule".
   Empty list is acceptable.

Respond with ONLY a single JSON object — no preamble, no markdown fences, no commentary."""


# ── Per-topic caps ─────────────────────────────────────────────────────
# Hard cap on a topic's share of the final summarize queue. Without this,
# broad-keyword Google News feeds (insurtech especially) can drown out
# substantive P&C content. Lowest-score items from over-cap topics are
# left in the kept-unsummarized state — still visible in the daily note,
# but don't burn MLX time on shallow items.
TOPIC_CAP_PCT: dict[str, float] = {
    "ai_insurtech": 0.35,
}


USER_TEMPLATE = """Source: {source}
Title: {title}
Author: {author}
Published: {published}
URL: {url}
Pre-assigned topic from triage: {topic_hint}

Content:
{content}

JSON only:"""


def _build_user_prompt(item: dict[str, Any]) -> str:
    content = (item.get("content") or "").strip()
    # Allow more context than triage since this is the premium step
    if len(content) > 6000:
        content = content[:6000] + "…[truncated]"
    if not content:
        content = "(no body content; reason from title and metadata only)"

    return USER_TEMPLATE.format(
        source=item.get("source", "?"),
        title=item.get("title", "?"),
        author=item.get("author") or "(unknown)",
        published=(item.get("published_at") or "")[:19],
        url=item.get("url") or "(no URL)",
        topic_hint=item.get("topic") or "(none)",
        content=content,
    )


# ── JSON parsing (shared) ──────────────────────────────────────────────


def _extract_json(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Greedy capture across whole string in case of multi-line JSON
    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _normalize_summary(parsed: dict[str, Any], fallback_topic: str) -> SummaryOutput:
    # Keep in sync with TOPICS in digest/triage.py
    valid_topics = {
        "cat_event", "reinsurance_cycle", "regulatory_rate", "underwriting_results",
        "reserving", "ma_capital", "climate_risk", "cyber", "social_inflation",
        "ai_insurtech", "distribution", "personal_lines", "commercial_specialty",
        "macro_linkage", "rates_cost_of_capital", "supply_chain", "analytics_modeling",
    }
    valid_confidence = {"low", "medium", "high"}

    topic = str(parsed.get("topic", fallback_topic)).lower().strip()
    if topic not in valid_topics:
        # Prefer triage's classification; otherwise default to macro_linkage
        # (broader catchall than the old "other"; better than mislabeling).
        topic = fallback_topic if fallback_topic in valid_topics else "macro_linkage"

    confidence = str(parsed.get("confidence", "medium")).lower().strip()
    if confidence not in valid_confidence:
        confidence = "medium"

    see_also_raw = parsed.get("see_also") or []
    if isinstance(see_also_raw, str):
        see_also_raw = [see_also_raw]
    see_also = [str(s).strip() for s in see_also_raw if str(s).strip()][:3]

    return SummaryOutput(
        topic=topic,
        summary=str(parsed.get("summary", "")).strip(),
        why_it_matters=str(parsed.get("why_it_matters", "")).strip(),
        confidence=confidence,
        see_also=see_also,
    )


# ── Backends ──────────────────────────────────────────────────────────


class BackendError(Exception):
    """Raised when a backend call fails for any reason."""


def _call_claude_cli(user_prompt: str) -> str:
    """Headless Claude Code via `claude -p`.

    Streams the system prompt + user prompt via stdin to avoid hitting
    shell argument-length limits, and uses the `--output-format json`
    flag so we get a stable JSON envelope back.
    """
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
    cmd = [
        "claude",
        "-p",
        "--model", settings.summarizer_model,
        "--output-format", "json",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=settings.summarizer_timeout_sec,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackendError(
            "`claude` CLI not on PATH. Install Claude Code or switch SUMMARIZER_BACKEND."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"claude CLI timeout after {settings.summarizer_timeout_sec}s") from exc

    if result.returncode != 0:
        raise BackendError(
            f"claude CLI exit {result.returncode}: {result.stderr.strip()[:500]}"
        )

    # `claude -p --output-format json` returns an envelope with a "result" field
    # that contains the assistant's text. Parse defensively.
    try:
        envelope = json.loads(result.stdout)
        text = envelope.get("result") or envelope.get("response") or result.stdout
    except json.JSONDecodeError:
        text = result.stdout
    return text


def _call_haiku_api(user_prompt: str) -> str:
    """Direct Anthropic API. Uses prompt caching on the system prompt."""
    if not settings.anthropic_api_key:
        raise BackendError("ANTHROPIC_API_KEY not set; cannot use haiku_api backend")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 600,
            "system": [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=settings.summarizer_timeout_sec,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _call_gemini_flash(user_prompt: str) -> str:
    if not settings.gemini_api_key:
        raise BackendError("GEMINI_API_KEY not set; cannot use gemini_flash_free backend")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )
    r = requests.post(
        url,
        headers={"x-goog-api-key": settings.gemini_api_key},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        },
        timeout=settings.summarizer_timeout_sec,
    )
    r.raise_for_status()
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _call_local_qwen(user_prompt: str) -> str:
    """Same Ollama instance as triage, just a richer prompt."""
    url = settings.ollama_host.rstrip("/") + "/api/generate"
    r = requests.post(
        url,
        json={
            "model": settings.ollama_model,
            "system": SYSTEM_PROMPT,
            "prompt": user_prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 600, "num_ctx": 8192},
        },
        timeout=settings.summarizer_timeout_sec,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def _call_mlx_local(user_prompt: str) -> str:
    """MLX-LM server (Apple Silicon). OpenAI-compatible endpoint.

    Start the server before running digest:
        /Users/dramsey/.venvs/mlx/bin/mlx_lm.server \\
            --model mlx-community/Qwen3.5-27B-4bit --port 8080

    Thinking mode is disabled via chat_template_kwargs so the model
    returns clean JSON without <think>...</think> blocks.
    """
    url = settings.mlx_server_url.rstrip("/") + "/v1/chat/completions"
    server_url = settings.mlx_server_url  # always localhost — no credentials to strip
    try:
        r = requests.post(
            url,
            json={
                "model": settings.mlx_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 600,
                "temperature": 0.2,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=settings.summarizer_timeout_sec,
        )
        r.raise_for_status()
    except requests.ConnectionError as exc:
        raise BackendError(
            f"MLX server not reachable at {server_url}. "
            "Start it with: mlx_lm.server --model mlx-community/Qwen3.5-27B-4bit --port 8080"
        ) from exc
    except requests.Timeout as exc:
        raise BackendError(
            f"MLX server timed out after {settings.summarizer_timeout_sec}s at {server_url}. "
            "Server may have crashed — check logs and restart with: "
            "mlx_lm.server --model mlx-community/Qwen3.5-27B-4bit --port 8080"
        ) from exc
    choices = r.json().get("choices", [])
    if not choices:
        raise BackendError(f"MLX server returned no choices: {r.text[:300]}")
    return choices[0].get("message", {}).get("content", "")


BACKENDS = {
    "claude_cli_pro":     _call_claude_cli,
    "haiku_api":          _call_haiku_api,
    "gemini_flash_free":  _call_gemini_flash,
    "local_qwen":         _call_local_qwen,
    "mlx_local":          _call_mlx_local,
}


# ── Public API ─────────────────────────────────────────────────────────


def summarize_item(item: dict[str, Any], regime_framing: str = "") -> SummaryOutput:
    """Summarize one item using the configured backend. Raises BackendError on failure."""
    backend_name = settings.summarizer_backend
    backend_fn = BACKENDS.get(backend_name)
    if not backend_fn:
        raise BackendError(
            f"Unknown SUMMARIZER_BACKEND: {backend_name!r}. "
            f"Valid: {sorted(BACKENDS.keys())}"
        )

    user_prompt = _build_user_prompt(item)
    if regime_framing:
        user_prompt = f"[Macro regime: {regime_framing}]\n\n{user_prompt}"
    raw = backend_fn(user_prompt)
    parsed = _extract_json(raw)
    if not parsed:
        raise BackendError(
            f"Backend {backend_name} returned unparseable output: {raw[:300]!r}"
        )
    return _normalize_summary(parsed, fallback_topic=item.get("topic") or "other")


def _enforce_topic_caps(
    rows: list,
    caps: dict[str, float],
) -> tuple[list, dict[str, int]]:
    """Drop lowest-score items from over-cap topics so each capped topic's
    share of the final queue stays ≤ its max_pct.

    Args:
        rows: queue from items_ready_for_summary, sorted by triage_score desc.
        caps: {topic_slug: max_pct} where max_pct is the maximum fraction of
              the final queue this topic may occupy (0.35 = 35%).

    Returns: (filtered_rows_in_original_order, {topic: count_dropped}).
    """
    if not caps or not rows:
        return list(rows), {}

    by_topic: dict[str, list] = {}
    for r in rows:
        by_topic.setdefault(r["topic"] or "", []).append(r)

    keep_ids: set = {r["id"] for r in rows}
    dropped: dict[str, int] = {}

    for topic, max_pct in caps.items():
        if not 0.0 < max_pct < 1.0 or topic not in by_topic:
            continue
        target_count = len(by_topic[topic])
        other_count  = len(rows) - target_count
        # target ≤ max_pct × (target + other)  ⇒  target ≤ (max_pct / (1 - max_pct)) × other
        max_allowed = int((max_pct / (1.0 - max_pct)) * other_count)
        if target_count <= max_allowed:
            continue
        # Drop the lowest-score items from this topic.
        ranked = sorted(by_topic[topic], key=lambda r: r["triage_score"] or 0, reverse=True)
        to_drop = ranked[max_allowed:]
        dropped[topic] = len(to_drop)
        for r in to_drop:
            keep_ids.discard(r["id"])

    return [r for r in rows if r["id"] in keep_ids], dropped


def run_summarize(
    limit: int | None = None,
    source: str | None = None,
    uncapped: bool = False,
) -> dict[str, int]:
    """Summarize items that passed triage. Returns counts.

    Args:
        limit: explicit max rows; defaults to SUMMARIZER_MAX_PER_RUN.
        source: optional source filter (e.g. "clipped" to summarize only clips).
        uncapped: if True, ignore SUMMARIZER_MAX_PER_RUN entirely. Used for the
            clipped pass — clipped items are user-curated and should never be
            dropped just because the cap was hit.
    """
    if uncapped:
        cap: int | None = None
        per_source_cap: int | None = None
    elif limit is not None:
        cap = limit
        per_source_cap = None  # explicit limit overrides per-source cap
    else:
        cap = settings.summarizer_max_per_run
        per_source_cap = settings.summarizer_max_per_source if source is None else None
    rows = db.items_ready_for_summary(limit=cap, source=source, per_source_cap=per_source_cap)
    if not rows:
        logger.info("summarize: nothing ready (source=%s)", source or "all")
        return {"ready": 0, "succeeded": 0, "failed": 0}

    # Apply per-topic share caps so a noisy keyword feed doesn't drown
    # substantive items. Items dropped here remain triage=keep and will
    # show up in the kept-unsummarized section of the daily note.
    rows, dropped_by_topic = _enforce_topic_caps(rows, TOPIC_CAP_PCT)
    if dropped_by_topic:
        for topic, n in dropped_by_topic.items():
            logger.info(
                "summarize: topic cap dropped %d %s items (kept-unsummarized)",
                n, topic,
            )

    backend = settings.summarizer_backend

    # Pre-flight: verify MLX server can actually generate (not just respond to HTTP).
    # Uses a tiny 1-token inference with a 20s timeout to detect hung generation threads.
    if backend == "mlx_local":
        probe_url = settings.mlx_server_url.rstrip("/") + "/v1/chat/completions"
        try:
            probe = requests.post(probe_url, json={
                "model": settings.mlx_model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=20)
            probe.raise_for_status()
        except requests.ConnectionError:
            logger.error(
                "summarize: MLX server not reachable at %s — start it first, skipping batch",
                settings.mlx_server_url,
            )
            return {"ready": len(rows), "succeeded": 0, "failed": 0}
        except (requests.Timeout, Exception) as exc:
            logger.error(
                "summarize: MLX server health-check failed (%s) — "
                "server may have crashed, skipping batch. "
                "Restart with: mlx_lm.server --model mlx-community/Qwen3.5-27B-4bit --port 8080",
                exc,
            )
            return {"ready": len(rows), "succeeded": 0, "failed": 0}

    # Wave 1: no regime framing yet. Wave 2 will reintroduce when the P&C
    # market-cycle + CAT-load regime detector lands.
    regime_framing = ""

    counts = {"ready": len(rows), "succeeded": 0, "failed": 0}
    for row in rows:
        item = dict(row)
        item_id = item["id"]
        t0 = time.perf_counter()
        input_chars = len(item.get("content") or "")
        status = "ok"
        error_msg: str | None = None
        output_chars = 0

        try:
            output = summarize_item(item, regime_framing=regime_framing)
            db.update_summary(
                item_id=item_id,
                topic=output.topic,
                summary=output.summary,
                why_it_matters=output.why_it_matters,
                confidence=output.confidence,
                see_also=output.see_also,
            )
            output_chars = len(output.summary) + len(output.why_it_matters)
            counts["succeeded"] += 1
            logger.info(
                "summarize: id=%d topic=%s confidence=%s (%.1fs)",
                item_id, output.topic, output.confidence,
                time.perf_counter() - t0,
            )
        except BackendError as exc:
            status = "error"
            error_msg = str(exc)[:500]
            counts["failed"] += 1
            logger.error("summarize: id=%d failed: %s", item_id, error_msg)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_msg = f"{type(exc).__name__}: {exc}"[:500]
            counts["failed"] += 1
            logger.exception("summarize: id=%d crashed", item_id)
        finally:
            db.log_summarizer(
                backend=backend,
                item_id=item_id,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                input_chars=input_chars,
                output_chars=output_chars,
                status=status,
                error=error_msg,
            )

    return counts
