"""Triage — Ollama Qwen2.5:14b decides keep/drop, assigns a P&C topic.

Filters the firehose to ~15-25 items per run that warrant MLX-quality
summarization. Python auto-keep hook runs first (EDGAR 8-K from named
insurer tickers, locked by design choice — see project memory) so a
model misread cannot silently drop a material disclosure.
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from typing import Any

import requests

from digest import db
from digest.config import settings
from digest_core.summarize.runner import extract_json

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "{host}/api/generate"
REQUEST_TIMEOUT_SEC = 60

# 17 P&C topics + sub_tags for sub-classification. Triage maps each kept
# item to exactly one topic; sub_tags is a list (often empty).
TOPICS = [
    "cat_event",
    "reinsurance_cycle",
    "regulatory_rate",
    "underwriting_results",
    "reserving",
    "ma_capital",
    "climate_risk",
    "cyber",
    "social_inflation",
    "ai_insurtech",
    "distribution",
    "personal_lines",
    "commercial_specialty",
    "macro_linkage",
    "rates_cost_of_capital",
    "supply_chain",
    "analytics_modeling",
]
SUB_TAGS = ["litigation_tplf"]  # extend as new sub-classifications arise

# Wave 1 insurer ticker universe — items from EDGAR with form in
# {8-K, 10-K, 10-Q} and ticker in this set bypass Ollama via Python hook.
# BRK covers GEICO (Berkshire's auto-insurance subsidiary) via consolidated
# filings.
INSURER_TICKERS_WAVE1 = {
    "TRV", "ALL", "PGR", "CB", "HIG", "AIG", "MET", "PRU", "RNR",
    "EG",  "AXS", "MMC", "AON", "WTW", "BRK",
}
MANDATORY_FORM_TYPES = {"8-K", "10-K", "10-Q"}

SYSTEM_PROMPT = """You are the triage gate for a P&C insurance and financial services digest.
Decide whether an item is worth keeping for readers spanning four personas:
industry analyst, insurer investor, underwriter, broker / market-intel and AI/insurtech tracker.

TOPIC TAXONOMY — assign exactly one. Use sub_tags only where indicated.
======================================================================
cat_event              active named storms, EQ, severe convective storms, wildfire, flood; modeled losses
reinsurance_cycle      1/1, 4/1, 7/1 renewals; capacity; retro; ILS; reinsurer results
regulatory_rate        state DOI rate filings (SERFF), NAIC actions, FIO/Treasury
underwriting_results   combined ratio, loss/expense ratios, accident-year commentary
reserving              loss-reserve adequacy, adverse/favorable dev, IBNR, asbestos/PFAS
ma_capital             insurer M&A, IPO, raises, buybacks, dividends, takeovers
climate_risk           physical & transition risk, ESG, market exits (CA/FL/LA)
cyber                  cyber insurance market, breach impact on carriers, AI as attack surface
social_inflation       nuclear verdicts, severity inflation, tort reform
                       sub_tag litigation_tplf — TPLF funders, MDLs, attorney economics
ai_insurtech           AI in UW/claims, insurtech funding, MGAs, embedded insurance
distribution           broker M&A (MMC/AON/WTW/BRO/AJG/RYAN), agency networks
personal_lines         auto/home pricing, telematics, market exits, severity/frequency
commercial_specialty   E&S, workers comp, D&O, E&O, environmental, programs/MGAs
macro_linkage          CPI→loss costs, FX for global carriers, geopolitics, energy→CAT severity
rates_cost_of_capital  rate impact on investment income, insurer debt, cat-bond coupons, hurdle rates
supply_chain           auto parts/labor severity, contractor capacity, medical/Rx, cargo/marine BI
analytics_modeling     cat models (RMS/AIR/Verisk/KCC), pricing/reserving methods, CAS research

AUTO-KEEP IN-PROMPT (set decision=keep, confidence=high)
========================================================
- AM Best, S&P, Moody's, Fitch rating actions on an insurer/reinsurer
- NAIC model-law adoption, market-conduct action, or capital framework change
- State DOI / SERFF rate filings with a requested change ≥ 5%
- USGS earthquake M ≥ 6.0 in a U.S. state or U.S. territory (non-U.S. quakes are
  drop — this digest tracks U.S. insurance exposure only)

AUTO-DISCARD (set decision=drop, score=0.0)
===========================================
- Personal-finance / retail-investor advice (how to pick auto insurance, save on premiums)
- Life / health / annuity content with no P&C linkage — this digest is P&C ONLY
- Generic business news mentioning an insurer only in passing (sponsorship, hiring,
  office relocation) without operational or financial substance
- Press releases about earnings dates, conference attendance, or routine personnel
  moves below C-suite or chief actuary
- Non-U.S./Caribbean tropical-cyclone reporting (e.g. the Chinese "National
  Hurricane Center" / NHC China) without explicit U.S. landfall risk or
  Caribbean threat — this digest tracks U.S. insurance exposure only
- Non-U.S. earthquakes / seismic events (Chile, Indonesia, Japan, etc.) — no
  matter the magnitude, drop unless the place names a U.S. state or U.S.
  territory (Puerto Rico, U.S. Virgin Islands, Guam, American Samoa,
  Northern Mariana Islands)
- Cat events outside the United States more broadly — floods, wildfires,
  storms, EQ in non-U.S. locales without U.S. carrier exposure
- Generic travel-volume reporting (TSA throughput, AAA holiday-travel forecasts,
  airline passenger counts) without an insurance loss or claims angle
- Road / highway / general infrastructure funding policy without an auto,
  commercial-auto, or workers-comp linkage
- General AI / LLM model releases, capability benchmarks, or AI-vendor PR with
  no clear insurance underwriting, claims, distribution, or pricing use case

SCORING (for non-auto items)
============================
score in [0.0, 1.0]:
  0.9-1.0  Material event for carrier P&L, reserves, capital, or capacity
  0.7-0.9  Notable industry development with multi-carrier or systemic implications
  0.5-0.7  Single-carrier development with limited spillover, or thoughtful analysis
  0.3-0.5  Routine industry color, useful for context but not actionable
  0.0-0.3  Marginal relevance; would be discarded by a busy reader

confidence in {high, medium, low}: how sure are you the topic and score are right
  high    source is authoritative AND content is unambiguous
  medium  source is reliable but content requires interpretation
  low     source quality is mixed OR content is speculative/rumor

REGULATORY SONAR — burden classification (regulatory_rate items only)
=====================================================================
For items you assign topic=regulatory_rate, also fill burden_direction and
burden_intensity. They describe how this oversight action shifts the cost or
operational burden on US P&C insurers. Use null for every other topic.

burden_direction:
  increasing  — tightens oversight, raises insurer burden (rate suppression,
                expanded liability statutes, mandated coverage, anti-redlining
                rules, climate mandates, FAIR Plan assessment expansions)
  decreasing  — loosens oversight, reduces insurer burden (rate flexibility,
                tort reform, FAIR Plan offload, federal preemption favoring
                carriers, mandated coverage repeal)
  neutral     — administrative, procedural, or symbolic (process changes,
                routine filings without material impact, conference statements)

burden_intensity:
  high    — affects a top-5 P&C market (CA, FL, TX, NY, LA) OR sweeps multiple
            states OR fundamentally reprices a major line OR sets binding
            federal precedent
  medium  — single-state material change, single-line repricing, or a notable
            committee action likely to shape future rulemaking
  low     — routine filing, narrow scope, predictable rate change, or one of
            many comparable actions

state (regulatory_rate items only): the two-letter US state code the action
applies to (CA, FL, TX, NY, LA, …). Use null for multi-state/federal actions
and for every non-regulatory_rate item. This lets the digest track per-state
regulatory burden.

OUTPUT — JSON only, no prose, no markdown fences
================================================
{
  "decision":         "keep" | "drop",
  "score":            float 0.0-1.0,
  "topic":            one of the 17 topics above,
  "sub_tags":         [] or ["litigation_tplf"],
  "confidence":       "high" | "medium" | "low",
  "reason":           string, max 50 words; cite the specific signal driving the call,
  "burden_direction": "increasing" | "neutral" | "decreasing" | null,
  "burden_intensity": "high" | "medium" | "low" | null,
  "state":            "CA" | "FL" | … | null
}"""

# Valid two-letter US state / DC codes for the Lead 9 `state` field.
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)

USER_TEMPLATE = """Source:    {source}
Title:     {title}
Author:    {author}
Published: {published}
Hint:      {topic_hint}

Content excerpt:
{content}

JSON verdict:"""


def _build_prompt(item: dict[str, Any]) -> str:
    metadata = json.loads(item.get("metadata_json") or "{}")
    content = (item.get("content") or "").strip()
    if len(content) > 600:
        content = content[:600] + "…"
    if not content:
        content = "(no body content; title-only)"

    return USER_TEMPLATE.format(
        source=item.get("source", "?"),
        title=item.get("title", "?"),
        author=item.get("author") or "(unknown)",
        published=(item.get("published_at") or "")[:19],
        topic_hint=metadata.get("topic_hint") or metadata.get("group") or "(none)",
        content=content,
    )


def _normalize_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    decision = str(verdict.get("decision", "drop")).lower().strip()
    if decision not in ("keep", "drop"):
        decision = "drop"

    raw_score = verdict.get("score", 0.0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    if decision == "keep" and score < settings.triage_min_score:
        decision = "drop"

    topic = str(verdict.get("topic", "macro_linkage")).lower().strip()
    if topic not in TOPICS:
        topic = "macro_linkage"  # safer default than 'other' for P&C content

    raw_sub_tags = verdict.get("sub_tags") or []
    if isinstance(raw_sub_tags, str):
        raw_sub_tags = [raw_sub_tags]
    sub_tags = [t for t in raw_sub_tags if t in SUB_TAGS]

    confidence = str(verdict.get("confidence", "medium")).lower().strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    # Regulatory Sonar lite — burden fields populated only for regulatory_rate.
    burden_direction: str | None = None
    burden_intensity: str | None = None
    state: str | None = None
    if topic == "regulatory_rate":
        raw_dir = str(verdict.get("burden_direction") or "").lower().strip()
        if raw_dir in ("increasing", "neutral", "decreasing"):
            burden_direction = raw_dir
        raw_int = str(verdict.get("burden_intensity") or "").lower().strip()
        if raw_int in ("high", "medium", "low"):
            burden_intensity = raw_int
        raw_state = str(verdict.get("state") or "").upper().strip()
        if raw_state in _US_STATES:                 # Lead 9; multi-state/federal → null
            state = raw_state

    return {
        "decision":         decision,
        "score":            score,
        "topic":            topic,
        "sub_tags":         sub_tags,
        "confidence":       confidence,
        "reason":           str(verdict.get("reason", ""))[:400],  # ~50 words ≈ 350 chars
        "burden_direction": burden_direction,
        "burden_intensity": burden_intensity,
        "state":            state,
    }


def _ollama_call(prompt: str) -> str:
    url = OLLAMA_GENERATE_URL.format(host=settings.ollama_host.rstrip("/"))
    payload = {
        "model":  settings.ollama_model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": 384,   # bumped from 256 to fit 50-word reason + JSON
            "num_ctx":     4096,
        },
    }
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json().get("response", "")


_DEDUP_THRESHOLD = 0.85


def _dedup_match(title: str, seen: list[str]) -> str | None:
    t = title.lower()
    for s in seen:
        if difflib.SequenceMatcher(None, t, s.lower()).ratio() >= _DEDUP_THRESHOLD:
            return s
    return None


def triage_item(item: dict[str, Any]) -> dict[str, Any]:
    """Run triage on one item. Returns the normalized verdict."""
    prompt = _build_prompt(item)
    raw = _ollama_call(prompt)
    verdict = extract_json(raw) or {}
    if not verdict:
        logger.warning("triage: failed to parse Qwen output for item %s", item.get("id"))
        return {
            "decision":         "drop",
            "score":            0.0,
            "topic":            "macro_linkage",
            "sub_tags":         [],
            "confidence":       "low",
            "reason":           "parse_error",
            "burden_direction": None,
            "burden_intensity": None,
            "state":            None,
        }
    return _normalize_verdict(verdict)


def run_triage(limit: int = 200) -> dict[str, int]:
    """Triage all pending items (up to `limit`)."""
    # Python auto-keep hooks — mandatory cases that cannot silently fail.
    auto_kept = db.auto_keep_insurer_filings(
        tickers=INSURER_TICKERS_WAVE1,
        form_types=MANDATORY_FORM_TYPES,
    )
    if auto_kept:
        logger.info("triage: auto-kept %d insurer filings (bypassing Ollama)", auto_kept)

    # (fn, label) pairs — keeps the ordering visible without 8 copy-paste log blocks.
    _AUTO_KEEP_HOOKS = [
        (db.auto_keep_nhc_advisories,       "NHC storm advisories"),
        (db.auto_keep_usgs_major,           "USGS M≥6.0 earthquakes"),
        (db.auto_keep_quantitative,         "quantitative items (FRED/etc)"),
        (db.auto_keep_courtlistener_dockets, "CourtListener MDL dockets"),
        (db.auto_keep_state_doi,            "state DOI press releases"),
        (db.auto_keep_serff,                "SERFF rate filings ≥5%%"),
        (db.auto_keep_investor_supp,        "investor-supplement tables"),
        (db.auto_keep_naic_schedp,          "NAIC Schedule P triangles"),
    ]
    for hook, label in _AUTO_KEEP_HOOKS:
        n = hook()
        if n:
            logger.info("triage: auto-kept %d %s", n, label)
            auto_kept += n

    items = db.items_needing_triage(limit=limit)
    if not items:
        logger.info("triage: nothing pending")
        return {"pending": 0, "kept": auto_kept, "dropped": 0, "errors": 0}

    seen_titles = db.recent_kept_titles(hours=24)

    counts = {"pending": len(items), "kept": auto_kept, "dropped": 0, "errors": 0}
    for row in items:
        item_dict = dict(row)
        title = item_dict.get("title") or ""
        try:
            match = _dedup_match(title, seen_titles)
            if match:
                db.update_triage(
                    item_id=item_dict["id"],
                    decision="drop",
                    score=0.0,
                    topic="macro_linkage",
                    source=item_dict.get("source"),
                    source_id=item_dict.get("source_id"),
                )
                counts["dropped"] += 1
                logger.info(
                    "triage: id=%d drop/dedup — matches: %.60s",
                    item_dict["id"], match,
                )
                continue

            t0 = time.perf_counter()
            verdict = triage_item(item_dict)
            elapsed = time.perf_counter() - t0
            db.update_triage(
                item_id=item_dict["id"],
                decision=verdict["decision"],
                score=verdict["score"],
                topic=verdict["topic"],
                burden_direction=verdict.get("burden_direction"),
                burden_intensity=verdict.get("burden_intensity"),
                sub_tags=verdict.get("sub_tags"),
                source=item_dict.get("source"),
                source_id=item_dict.get("source_id"),
                state=verdict.get("state"),
            )
            if verdict["decision"] == "keep":
                counts["kept"] += 1
                seen_titles.append(title)
            else:
                counts["dropped"] += 1
            burden_part = (
                f" burden={verdict['burden_direction']}/{verdict['burden_intensity']}"
                if verdict.get("burden_intensity") else ""
            )
            logger.info(
                "triage: id=%d %s/%.2f topic=%s sub=%s conf=%s%s (%.1fs) — %s",
                item_dict["id"],
                verdict["decision"],
                verdict["score"],
                verdict["topic"],
                verdict["sub_tags"],
                verdict["confidence"],
                burden_part,
                elapsed,
                verdict.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.exception("triage: id=%s failed: %s", item_dict.get("id"), exc)
    return counts
