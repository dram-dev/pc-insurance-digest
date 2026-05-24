"""Wave 2 — Signal leaderboard.

Scores every kept+summarized item with a per-item leaderboard formula:

    score = source_mult × regime_mult × topic_relevance × recency
          × llm_judgment × topic_priority_boost × burden_intensity_boost

The output drives:
  * Top-5 callout in the daily note
  * Top-15 + per-source quality table in the weekly note
  * Eventual `_meta/Leaderboard.md` rolling 30d view

Components, in plain English:
  source_mult           Trust the channel (EDGAR > AM Best > trade press > Reddit > HN).
  regime_mult           Current market-cycle × cat-load multiplier from the regime detector.
  topic_relevance       Reserved — currently 1.0 for every topic. Tune later if topic
                        emphasis under specific regimes needs sharpening (e.g. cat_event
                        under post_major_event, reinsurance_cycle under hard_market).
  recency               Linear half-life over 7 days, floor 0.3.
  llm_judgment          Materiality from summarize.py (0.5–1.5), default 1.0 if missing.
  topic_priority_boost  Personal-lines auto + homeowners/fire = 1.3, others 1.0.
  burden_intensity_boost  Regulatory Sonar lite — placeholder 1.0 until Wave 2.x ships
                          burden_intensity classification on regulatory_rate items.

Persistence:
  * Each `digest signals` run inserts one row per item into `signal_scores`
    keyed by (item_id, computed_at). Older rows are retained so we can see
    drift over time.
  * `digest signals --display` reads the latest row per item.
"""
from __future__ import annotations

import logging
import math
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
    "personal_lines": 1.3,
}


# ── Burden intensity boost (Wave 2 lite — populated when Sonar ships) ──

BURDEN_INTENSITY_BOOST: dict[str, float] = {
    "high":   1.3,
    "medium": 1.1,
    "low":    1.0,
}


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

    def as_row(self, computed_at: str) -> dict[str, Any]:
        return {
            "item_id":         self.item_id,
            "computed_at":     computed_at,
            "score":           self.score,
            "source_mult":     self.source_mult,
            "regime_mult":     self.regime_mult,
            "topic_relevance": self.topic_relevance,
            "recency":         self.recency,
            "llm_judgment":    self.llm_judgment,
            "topic_boost":     self.topic_boost,
            "burden_boost":    self.burden_boost,
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

    score = (
        src_mult * rg_mult * topic_rel * rec
        * llm_j * topic_boost * burden_boost
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
