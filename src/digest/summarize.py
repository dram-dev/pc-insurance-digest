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
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from digest import db
from digest.config import settings
from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError
from digest_core.summarize.runner import extract_json

logger = logging.getLogger(__name__)


# ── Output schema ──────────────────────────────────────────────────────


@dataclass
class SummaryOutput:
    topic: str                       # canonical topic from triage taxonomy
    summary: str                     # 2-3 sentences
    why_it_matters: str              # 1-2 sentences, user-specific framing
    confidence: str                  # "low" | "medium" | "high"
    see_also: list[str] = field(default_factory=list)
    materiality: float = 1.0         # 0.5-1.5, feeds Wave 2 leaderboard llm_judgment


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
6. "materiality": a float in [0.5, 1.5] gauging how material this item is to a P&C reader who
   prioritizes personal-lines auto and homeowners/fire signal. Anchor your score on these guides
   and ERR HIGH when in doubt — historically the summarizer has under-scored systemic moves:
     1.5  Severe near-term financial/operational impact. Examples that REQUIRE 1.5:
          - "biggest/largest/highest in N years" industry-wide P&L or combined-ratio data
          - top-5-state (CA/FL/TX/NY/LA) DOI rate action ≥10%, FAIR Plan / Citizens action,
            tort reform bill passage, statewide market exit
          - active hurricane landfall; M≥6.0 EQ in populated area; multi-billion CAT estimate
          - nuclear verdict ≥$50M or precedent-setting MDL ruling
          - major reinsurer insolvency or capital raise ≥$500M
     1.4  Industry-wide signal short of "record": multi-carrier rate actions, regional market
          dislocation, single-state regulatory shift in a top-5 state, broker M&A
          consolidation creating a top-10 national platform.
     1.2  Notable industry development with multi-carrier or systemic implications
          (single-carrier guidance, reinsurer commentary across renewals, Substack analyst
          deep-dive on a structural trend).
     1.0  Standard substantive item — single-carrier action, single-state routine filing,
          analyst note covering one company.
     0.8  Marginal — adjacent news, weak primary signal, mostly context.
     0.5  Barely material — included for completeness, easily skipped.

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

    try:
        materiality = float(parsed.get("materiality", 1.0))
    except (TypeError, ValueError):
        materiality = 1.0
    materiality = max(0.5, min(1.5, materiality))

    return SummaryOutput(
        topic=topic,
        summary=str(parsed.get("summary", "")).strip(),
        why_it_matters=str(parsed.get("why_it_matters", "")).strip(),
        confidence=confidence,
        see_also=see_also,
        materiality=materiality,
    )


# ── Backends ──────────────────────────────────────────────────────────


def _backend_config() -> BackendConfig:
    """Build the shared core BackendConfig from PC settings.

    haiku/gemini model names fall through to the core defaults; the rest map
    PC's .env-driven settings onto the transport layer.
    """
    return BackendConfig(
        timeout_sec=settings.summarizer_timeout_sec,
        claude_model=settings.summarizer_model,
        anthropic_api_key=settings.anthropic_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_host=settings.ollama_host,
        ollama_model=settings.ollama_model,
        mlx_server_url=settings.mlx_server_url,
        mlx_model=settings.mlx_model,
    )


# ── Public API ─────────────────────────────────────────────────────────


# ── EDGAR stub-summarize (no MLX call when filing body is empty) ───────


_EDGAR_STUB_TOPICS = {
    "8-K":    "underwriting_results",
    "10-Q":   "underwriting_results",
    "10-K":   "underwriting_results",
    "13F-HR": "ma_capital",
}


def _maybe_stub_fred(item: dict[str, Any]) -> SummaryOutput | None:
    """Bypass LLM for FRED items — they're already structured prose.

    FRED ingestor emits self-contained content like "Series X rose Y% m/m
    (z=+Z.ZZσ)". The LLM has nothing to add here, so we promote content
    directly as summary and assign materiality from the z-score magnitude.
    """
    if item.get("source") != "fred":
        return None
    content = (item.get("content") or "").strip()
    if not content:
        return None
    try:
        metadata = json.loads(item.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    z = abs(float(metadata.get("z_score") or 0.0))
    # 1.5σ → 1.1 (just over routine); 2σ → 1.2; 3σ → 1.4; 4σ+ → 1.5
    if   z >= 4.0: materiality = 1.5
    elif z >= 3.0: materiality = 1.4
    elif z >= 2.0: materiality = 1.2
    else:          materiality = 1.1
    label = metadata.get("label") or "FRED series"
    direction = "rose" if (metadata.get("mom_pct") or 0) > 0 else "fell"
    why = (
        f"{label} {direction} {abs(metadata.get('mom_pct', 0)):.2f}% m/m "
        f"({z:.2f}σ vs trailing 12m). Higher cost driver typically flows "
        f"into P&C personal-auto and homeowners severity within 1-2 quarters."
    )
    return SummaryOutput(
        topic="supply_chain",
        summary=content,
        why_it_matters=why,
        confidence="high",
        see_also=[],
        materiality=materiality,
    )


def _maybe_stub_insurer_filing(item: dict[str, Any]) -> SummaryOutput | None:
    """Bypass the LLM for EDGAR filings with no fetched body content.

    Without body text the summarizer hallucinates topics from the title alone
    (e.g., "TRV 10-Q filed ..." → ai_insurtech). Emit a deterministic stub
    keyed by form type instead. Materiality 0.9 is just below an LLM-judged
    standard item (1.0); the per-item leaderboard's carrier-priority boost
    will still surface big-3 carriers above generic press coverage.
    """
    if item.get("source") != "edgar":
        return None
    if (item.get("content") or "").strip():
        return None
    try:
        metadata = json.loads(item.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return None
    form   = metadata.get("form") or ""
    ticker = metadata.get("ticker") or ""
    if not ticker or form not in _EDGAR_STUB_TOPICS:
        return None

    topic = _EDGAR_STUB_TOPICS[form]
    if form == "13F-HR":
        summary = f"{ticker} 13F-HR holdings update filed with SEC. Body parsing not performed."
        why = f"{ticker} institutional positions — review for sector rotation or concentrated bets."
    else:
        summary = (
            f"{ticker} {form} on file with SEC; body content unavailable. "
            "Open source link for full disclosure."
        )
        why = (
            f"{ticker} disclosure — carrier-level signal on P&L, reserves, or guidance. "
            "Body text not present; check filing index for material items."
        )

    return SummaryOutput(
        topic=topic,
        summary=summary,
        why_it_matters=why,
        confidence="low",
        see_also=[],
        materiality=0.9,
    )


def _maybe_stub_investor_supp(item: dict[str, Any]) -> SummaryOutput | None:
    """Bypass LLM for investor-supplement table items — content is the parsed
    table text from a known quarterly disclosure PDF. MLX would mangle the
    numeric structure; promote content directly as summary."""
    if item.get("source") != "investor_supp":
        return None
    content = (item.get("content") or "").strip()
    if not content:
        return None
    try:
        meta = json.loads(item.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        meta = {}
    ticker     = meta.get("ticker") or "?"
    name       = meta.get("name") or ticker
    year       = meta.get("year")
    quarter    = meta.get("quarter")
    table_type = (meta.get("table_type") or "table").replace("_", " ")

    summary = (
        f"{name} ({ticker}) Q{quarter} {year} {table_type} table from investor "
        f"supplement:\n\n{content}"
    )
    why = (
        f"{ticker} quarterly reserving disclosure ({table_type}). Adverse-development "
        "or pending-count growth here is the early-warning surface for severity "
        "inflation and loss-cost trends."
    )
    return SummaryOutput(
        topic="reserving",
        summary=summary,
        why_it_matters=why,
        confidence="high",
        see_also=[],
        materiality=0.9,
    )


def _maybe_stub_naic_schedp(item: dict[str, Any]) -> SummaryOutput | None:
    """Bypass LLM for NAIC Schedule P triangle items — content will be the
    line-of-business triangle as deterministic text once a data source is
    wired. Until then this never fires (ingestor no-ops)."""
    if item.get("source") != "naic_schedp":
        return None
    content = (item.get("content") or "").strip()
    if not content:
        return None
    try:
        meta = json.loads(item.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        meta = {}
    insurer = meta.get("insurer") or meta.get("name") or "?"
    lob     = meta.get("line_of_business") or "P&C line"
    year    = meta.get("statement_year") or meta.get("year") or "?"
    adverse = meta.get("adverse_dev_pct")

    summary = (
        f"{insurer} {year} Schedule P — {lob} loss-development triangle:\n\n{content}"
    )
    if adverse is not None:
        why = (
            f"{insurer} {lob} {adverse:+.1f}% reserve development on {year} statutory "
            "Schedule P. Adverse development on long-tail lines is the social-inflation "
            "tell; favorable development is balance-sheet ballast."
        )
        materiality = 1.2 if abs(float(adverse)) >= 5.0 else 1.0
    else:
        why = (
            f"{insurer} {year} Schedule P {lob} triangle on record. Inspect for "
            "adverse-development pattern across calendar years."
        )
        materiality = 1.0
    return SummaryOutput(
        topic="reserving",
        summary=summary,
        why_it_matters=why,
        confidence="high",
        see_also=[],
        materiality=materiality,
    )


def summarize_item(item: dict[str, Any], regime_framing: str = "") -> SummaryOutput:
    """Summarize one item using the configured backend. Raises BackendError on failure."""
    # Short-circuits: deterministic stubs for structured-data sources that
    # don't benefit from MLX rewriting.
    stub = _maybe_stub_fred(item)
    if stub is not None:
        return stub
    stub = _maybe_stub_insurer_filing(item)
    if stub is not None:
        return stub
    stub = _maybe_stub_investor_supp(item)
    if stub is not None:
        return stub
    stub = _maybe_stub_naic_schedp(item)
    if stub is not None:
        return stub

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
    raw = backend_fn(SYSTEM_PROMPT, user_prompt, _backend_config())
    parsed = extract_json(raw)
    if not parsed:
        raise BackendError(
            f"Backend {backend_name} returned unparseable output: {raw[:300]!r}"
        )
    return _normalize_summary(parsed, fallback_topic=item.get("topic") or "other")


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
    # Lead 8: a substantiated insurtech deal (real dollar amount) is exempt from
    # the ai_insurtech cap, so real capital events survive the keyword cull.
    from digest import capital_flows
    protected = capital_flows.run_capital_flows(rows)
    rows, dropped_by_topic = capital_flows.enforce_topic_caps_protected(
        rows, TOPIC_CAP_PCT, protected
    )
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

    # Wave 2: pull the current regime so the summarizer knows whether we're
    # in hard-market / post-major-event when scoring materiality.
    try:
        from digest.regime import current_regime
        regime_framing = current_regime().summary_line()
    except Exception as exc:  # noqa: BLE001
        logger.warning("summarize: regime lookup failed (%s); proceeding without framing", exc)
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
                source=item.get("source"),
                source_id=item.get("source_id"),
            )
            db.update_materiality(item_id, output.materiality)
            output_chars = len(output.summary) + len(output.why_it_matters)
            counts["succeeded"] += 1
            logger.info(
                "summarize: id=%d topic=%s confidence=%s materiality=%.2f (%.1fs)",
                item_id, output.topic, output.confidence, output.materiality,
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
                source=item.get("source"),
                source_id=item.get("source_id"),
            )

    return counts
