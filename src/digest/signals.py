"""Wave 2 — Signal leaderboard.

Scores every kept+summarized item with a per-item leaderboard formula:

    score = source_mult × regime_mult × topic_relevance × recency
          × llm_judgment × topic_priority_boost × burden_intensity_boost
          × insurer_priority_boost × inflation_keyword_boost
          × regulatory_action_boost

The output drives:
  * Top-5 callout in the daily note
  * Top-15 + per-source quality table in the weekly note
  * Eventual `_meta/Leaderboard.md` rolling 30d view

Components, in plain English:
  source_mult            Trust the channel (EDGAR > AM Best > trade press > Reddit > HN).
  regime_mult            Current market-cycle × cat-load multiplier from the regime detector.
  topic_relevance        Reserved — currently 1.0 for every topic. Tune later if topic
                         emphasis under specific regimes needs sharpening.
  recency                Linear half-life over 7 days, floor 0.3.
  llm_judgment           Materiality from summarize.py (0.5–1.5), default 1.0 if missing.
  topic_priority_boost   Personal-lines auto + liability topics (social inflation,
                         commercial specialty, reserving, supply chain) > 1.0.
  burden_intensity_boost Regulatory Sonar lite — burden_intensity classification on
                         regulatory_rate items only.
  insurer_priority_boost EDGAR items keyed on ticker: PGR/ALL/BRK = 1.5, TRV = 1.3, etc.
                         (User priority: largest personal-auto carriers must outrank
                          generic press.) Only fires for source=edgar.
  inflation_keyword_boost  Title/summary keyword scan for the user's tracked inflation
                           drivers (auto parts, construction cost, labor cost/supply,
                           verdict/judgement, severity, loss cost). 1.2× on hit, else 1.0.

Persistence:
  * Each `digest signals` run inserts one row per item into `signal_scores`
    keyed by (item_id, computed_at). Older rows are retained so we can see
    drift over time.
  * `digest signals --display` reads the latest row per item.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from digest import db
from digest.regime import current_regime, RegimeSignal

logger = logging.getLogger(__name__)


# ── Source multipliers (from CLAUDE.md "Source multipliers" table) ────

SOURCE_MULT_DEFAULT = 0.7

SOURCE_MULT: dict[str, float] = {
    # Tier 1.3 — primary disclosures + government hazard advisories
    "edgar":   1.3,
    "nhc":     1.3,
    # Tier 1.2 — Wave 3 placeholders (NAIC / state DOI / AM Best)
    "usgs":    1.2,   # treat structured hazard data like a primary source
    "fred":    1.2,   # quantitative cost-driver anomalies (already σ-gated)
    # Tier 1.0 — trade press (handled by RSS source rolled up via the "rss" tag)
    "rss":     1.0,
    "spc":     1.0,
    "nifc":    1.0,
    # Tier 0.9 — Substack longform
    "substack": 0.9,
    # Tier 0.7 — Reddit
    "reddit":  0.7,
    # Tier 0.6 — HN
    "hn":      0.6,
    # Clipped items the user self-curated bypass scoring tiers
    "clipped": 1.3,
}


# ── Topic priority boost (locked in CLAUDE.md) ────────────────────────

TOPIC_PRIORITY_BOOST: dict[str, float] = {
    "personal_lines":       1.3,
    # Liability-trend topics — user preference: outrank cat_event by default
    "social_inflation":     1.4,
    "commercial_specialty": 1.4,
    "reserving":            1.4,
    # Inflationary cost-driver feeds (auto parts, construction, labor, medical/Rx)
    "supply_chain":         1.4,
    # Industry-financial-state topics — under-weighted before Score Higher review
    "underwriting_results": 1.2,   # combined ratio, AY commentary, industry profitability
    "distribution":         1.2,   # broker M&A (MMC/AON/WTW/BRO/AJG/RYAN/Patriot)
    "regulatory_rate":      1.2,   # state DOI / SERFF / NAIC actions (stacks with burden_boost)
}


# ── Burden intensity boost (Wave 2 lite — populated when Sonar ships) ──

BURDEN_INTENSITY_BOOST: dict[str, float] = {
    "high":   1.3,
    "medium": 1.1,
    "low":    1.0,
}


# ── Insurer-priority boost (carrier-level weighting on EDGAR items) ───
#
# User priority: largest personal-auto carriers (PGR, ALL, GEICO/BRK) must
# outrank generic trade-press coverage. Applied only when source=='edgar'
# and metadata.ticker matches. GEICO is a Berkshire subsidiary, so BRK is
# the proxy ticker.

PRIORITY_INSURERS_BOOST: dict[str, float] = {
    "PGR": 1.5,   # personal auto leader
    "ALL": 1.5,   # personal lines #2
    "BRK": 1.5,   # GEICO parent
    "TRV": 1.3,   # commercial + bond / specialty bellwether
    "CB":  1.3,
    "HIG": 1.2,
    "AIG": 1.2,
}


# ── Inflation keyword boost (user-tracked cost drivers) ───────────────
#
# Cross-topic boost for items naming the loss-cost inflation drivers the
# user cares about. Compiled once at module import — scans title +
# summary + why_it_matters. 1.2× on any hit, 1.0× otherwise. Multiple
# hits don't stack (one keyword is enough signal).

_INFLATION_KEYWORDS = (
    r"\bauto[\s-]?parts?\b",
    r"\bconstruction (?:cost|labor|material)",
    r"\blabor (?:cost|supply|shortage|inflation|rate)",
    r"\bwage[s]? (?:inflation|growth|pressure)",
    r"\bmedical (?:cost|inflation|trend)",
    r"\bnuclear verdict",
    r"\b(?:verdict|judgement|judgment|settlement)s?\b",
    r"\btort reform",
    r"\bsocial inflation",
    r"\bloss cost",
    r"\bseverity (?:trend|inflation|increase)",
    r"\bpure premium",
    r"\bclaim severity",
    r"\bbody shop",
    r"\brepair cost",
    r"\bused[\s-]car",
    r"\blitigat(?:ion|ed) financ",   # third-party litigation funding
)

_INFLATION_RE = re.compile("|".join(_INFLATION_KEYWORDS), re.IGNORECASE)


# ── Regulatory / state-action keyword boost ───────────────────────────
#
# Items naming a top-5-state regulatory action, an insurer of last resort,
# or a SERFF rate filing get 1.2× even when the assigned topic isn't
# `regulatory_rate` (e.g., the CA FAIR Plan rate hike is classified
# `personal_lines` per the fire-content routing rule but is structurally
# a regulatory event). Stacks with `burden_intensity_boost` when both
# apply.

_REGULATORY_KEYWORDS = (
    r"\bFAIR Plan\b",                          # CA insurer of last resort
    r"\bCitizens (?:Property|Insurance)\b",    # FL / LA insurer of last resort
    r"\binsurer of last resort\b",
    r"\b(?:rate|premium) (?:filing|hike|increase|approval|reduction)\b",
    r"\b(?:DOI|Department of Insurance) (?:approves|denies|orders|files)",
    r"\bSERFF\b",
    r"\bNAIC (?:adopts|approves|votes|releases|publishes)\b",
    r"\b(?:CDI|California Department of Insurance)\b",
    r"\b(?:FLOIR|Florida Office of Insurance Regulation)\b",
    r"\b(?:TDI|Texas Department of Insurance)\b",
    r"\b(?:LDI|Louisiana Department of Insurance)\b",
    r"\bNYDFS\b",
    r"\btort reform (?:bill|act|law|legislation)\b",
    r"\b(?:Senate|House) (?:bill|insurance committee)\b",
    r"\bmarket conduct (?:action|examination|review)\b",
    r"\bproposed rate (?:change|increase|filing)\b",
)

_REGULATORY_RE = re.compile("|".join(_REGULATORY_KEYWORDS), re.IGNORECASE)


def _regulatory_action_boost(row: Any) -> float:
    """Return 1.2 if title/summary/why_it_matters names a state DOI action,
    insurer of last resort, or SERFF rate filing. Fires across topics so
    fire-routed personal_lines items with regulatory substance still benefit.
    """
    parts: list[str] = []
    for key in ("title", "summary", "why_it_matters"):
        if key in row.keys():
            val = row[key]
            if val:
                parts.append(str(val))
    blob = " ".join(parts)
    return 1.2 if blob and _REGULATORY_RE.search(blob) else 1.0


def _inflation_keyword_boost(row: Any) -> float:
    """Return 1.2 if title/summary/why_it_matters names an inflation driver."""
    parts: list[str] = []
    for key in ("title", "summary", "why_it_matters"):
        if key in row.keys():
            val = row[key]
            if val:
                parts.append(str(val))
    blob = " ".join(parts)
    return 1.2 if blob and _INFLATION_RE.search(blob) else 1.0


def _insurer_priority_boost(source: str, metadata_json: Any) -> float:
    """Return per-ticker boost for EDGAR items; 1.0 otherwise."""
    if source != "edgar" or not metadata_json:
        return 1.0
    try:
        meta = json.loads(metadata_json)
    except (TypeError, ValueError):
        return 1.0
    ticker = (meta.get("ticker") or "").upper().strip()
    # Normalize BRK.A / BRK.B / BRK-B → BRK for matching
    if ticker.startswith("BRK"):
        ticker = "BRK"
    return PRIORITY_INSURERS_BOOST.get(ticker, 1.0)


# ── Recency ────────────────────────────────────────────────────────────


def _recency(published_iso: str | None, ingested_iso: str | None, half_life_days: float = 7.0) -> float:
    """Linear decay over `half_life_days`, floored at 0.3.

    Uses published_at if present, otherwise ingested_at. Returns 1.0 for items
    less than ~1 day old and 0.3 for anything older than `half_life_days`.
    """
    raw = published_iso or ingested_iso
    if not raw:
        return 0.6
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.6
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
    decay = max(0.3, 1.0 - age_days / half_life_days)
    return round(decay, 3)


# ── Topic relevance (placeholder; tune in Wave 2.x) ───────────────────


def _topic_relevance(topic: str | None, regime: RegimeSignal) -> float:
    """Currently 1.0 for every topic — keeps the formula intact so we can
    enable topic-by-regime tuning later without a schema change.

    Where it'll go (commented for later wiring):
      regime.cat_load == "post_major_event":
          cat_event           → 1.3
          personal_lines      → 1.1   # consumer-impact angle
      regime.market_cycle in {"hard_market", "transitioning_to_hard"}:
          reinsurance_cycle   → 1.15
          underwriting_results→ 1.1
    """
    return 1.0


# ── Scoring ───────────────────────────────────────────────────────────


@dataclass
class Score:
    item_id: int
    score: float
    source_mult: float
    regime_mult: float
    topic_relevance: float
    recency: float
    llm_judgment: float
    topic_boost: float
    burden_boost: float
    insurer_boost: float
    inflation_boost: float
    regulatory_boost: float

    def as_row(self, computed_at: str) -> dict[str, Any]:
        return {
            "item_id":          self.item_id,
            "computed_at":      computed_at,
            "score":            self.score,
            "source_mult":      self.source_mult,
            "regime_mult":      self.regime_mult,
            "topic_relevance":  self.topic_relevance,
            "recency":          self.recency,
            "llm_judgment":     self.llm_judgment,
            "topic_boost":      self.topic_boost,
            "burden_boost":     self.burden_boost,
            "insurer_boost":    self.insurer_boost,
            "inflation_boost":  self.inflation_boost,
            "regulatory_boost": self.regulatory_boost,
        }


def score_item(row: Any, regime: RegimeSignal) -> Score:
    """Compute the leaderboard score for one item row."""
    source     = (row["source"] or "").lower()
    topic      = (row["topic"]  or "").lower()

    src_mult   = SOURCE_MULT.get(source, SOURCE_MULT_DEFAULT)
    rg_mult    = regime.multiplier
    topic_rel  = _topic_relevance(topic, regime)
    rec        = _recency(
        row["published_at"] if "published_at" in row.keys() else None,
        row["ingested_at"]  if "ingested_at"  in row.keys() else None,
    )

    materiality = row["materiality_score"] if "materiality_score" in row.keys() else None
    try:
        llm_j = float(materiality) if materiality is not None else 1.0
    except (TypeError, ValueError):
        llm_j = 1.0
    llm_j = max(0.5, min(1.5, llm_j))

    topic_boost  = TOPIC_PRIORITY_BOOST.get(topic, 1.0)

    burden_intensity = (
        row["burden_intensity"] if "burden_intensity" in row.keys() else None
    )
    burden_boost = BURDEN_INTENSITY_BOOST.get(
        (burden_intensity or "").lower(), 1.0
    )

    metadata_json = row["metadata_json"] if "metadata_json" in row.keys() else None
    insurer_boost    = _insurer_priority_boost(source, metadata_json)
    inflation_boost  = _inflation_keyword_boost(row)
    regulatory_boost = _regulatory_action_boost(row)

    score = (
        src_mult * rg_mult * topic_rel * rec
        * llm_j * topic_boost * burden_boost
        * insurer_boost * inflation_boost * regulatory_boost
    )
    return Score(
        item_id=int(row["id"]),
        score=round(score, 4),
        source_mult=round(src_mult, 3),
        regime_mult=round(rg_mult, 3),
        topic_relevance=round(topic_rel, 3),
        recency=rec,
        llm_judgment=round(llm_j, 3),
        topic_boost=round(topic_boost, 3),
        burden_boost=round(burden_boost, 3),
        insurer_boost=round(insurer_boost, 3),
        inflation_boost=round(inflation_boost, 3),
        regulatory_boost=round(regulatory_boost, 3),
    )


# ── Public API ────────────────────────────────────────────────────────


def run_signals() -> dict[str, int]:
    """Score every kept+summarized item against the current regime; persist."""
    regime = current_regime()
    rows   = db.items_for_signals()
    if not rows:
        logger.info("signals: nothing to score")
        return {"scored": 0}

    computed_at = datetime.now(timezone.utc).isoformat()
    scored: list[dict[str, Any]] = []
    for row in rows:
        s = score_item(row, regime)
        scored.append(s.as_row(computed_at))

    inserted = db.upsert_signal_scores(scored)
    logger.info(
        "signals: scored %d items at regime ×%.2f (inserted=%d)",
        len(scored), regime.multiplier, inserted,
    )
    return {"scored": len(scored)}


def top_n(n: int = 5, since_iso: str | None = None) -> list[Any]:
    """Convenience: top-N items by latest score, since `since_iso`."""
    return db.top_signal_scores(limit=n, since_iso=since_iso)
