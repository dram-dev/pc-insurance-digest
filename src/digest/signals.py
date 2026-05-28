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
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest import db
from digest.config import settings
from digest.regime import current_regime, RegimeSignal

logger = logging.getLogger(__name__)


# ── Source multipliers (defaults; user can override via _meta/Scoring Weights.md) ────

SOURCE_MULT_DEFAULT = 0.7

SOURCE_MULT: dict[str, float] = {
    # Tier 1.3 — primary disclosures + government hazard advisories
    "edgar":   1.3,
    "nhc":     1.3,
    "clipped": 1.3,   # user self-curated, bypasses scoring tiers
    # Tier 1.2 — structured / regulatory primaries
    "usgs":             1.2,
    "fred":             1.2,
    "courtlistener":    1.2,   # federal docket primary source (MDL filings)
    "state_doi":        1.2,
    "serff":            1.2,
    "naic_schedp":      1.2,
    # Tier 1.1 — quarterly carrier disclosures
    "investor_supp":    1.1,
    # Tier 1.0 — trade press, curated reports, structured government datasets
    "rss":              1.0,
    "spc":              1.0,
    "nifc":             1.0,
    "collision":        1.0,
    "industry_research": 1.0,
    # Tier 0.9 — Substack longform
    "substack": 0.9,
    # Tier 0.7 — Reddit
    "reddit":  0.7,
    # Tier 0.6 — HN
    "hn":      0.6,
}


# ── Topic priority boost (defaults; user can override) ────────────────

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


# ── Conviction tier (high/medium/low) — adapted from macro-ai-digest ──
#
# macro-ai-digest clamps its score to [0,1] and tiers on fixed 0.72/0.40
# cutoffs. PC's leaderboard score is an unbounded product of ~11 multipliers
# centered on a neutral baseline of ~1.0 (average source, neutral regime, fresh,
# materiality 1.0, no boosts ≈ 1.0). So PC tiers anchor to that baseline: an
# item has to clear it by stacking real signal (strong source + materiality +
# boosts) to read "high". Thresholds are user-tunable via the `signal_tiers`
# section of _meta/Scoring Weights.md — recalibrate once live score
# distributions are visible on the Mac mini.

SIGNAL_TIER_DEFAULTS: dict[str, float] = {"high": 1.6, "medium": 0.9}
TIER_EMOJI: dict[str, str] = {"high": "🔴", "medium": "🟡", "low": "🔵"}
TIER_LABEL: dict[str, str] = {"high": "High", "medium": "Medium", "low": "Low"}


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


# ── User-editable weights (Obsidian _meta/Scoring Weights.md) ─────────
#
# The dict constants above are baked-in defaults. The user can override
# any value by editing the YAML frontmatter of `Scoring Weights.md` in
# the Obsidian vault's `_meta/` folder. The file is re-read at scoring
# time when its mtime changes — no Python edits needed for tuning.
#
# Missing file → use defaults. Malformed YAML → use defaults, warning
# logged. Unknown keys in overrides are ignored. Non-numeric values
# are ignored with a warning. The pipeline never breaks on a bad edit.

_DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "sources":          dict(SOURCE_MULT),
    "topics":           dict(TOPIC_PRIORITY_BOOST),
    "insurer_priority": dict(PRIORITY_INSURERS_BOOST),
    "keyword_boosts":   {"inflation": 1.2, "regulatory": 1.2, "tplf": 1.3},
    "burden_intensity": dict(BURDEN_INTENSITY_BOOST),
    "signal_tiers":     dict(SIGNAL_TIER_DEFAULTS),
}

# Cache shape: (path, mtime, weights). Re-read only when the file's
# mtime changes; the helpers can call _load_scoring_weights() freely.
_WEIGHTS_CACHE: tuple[Path | None, float, dict[str, dict[str, float]]] | None = None


def _scoring_weights_path() -> Path | None:
    if not settings.obsidian_vault_path:
        return None
    return (
        Path(settings.obsidian_vault_path)
        / settings.obsidian_digest_dir
        / "_meta"
        / "Scoring Weights.md"
    )


def _read_overrides(path: Path | None) -> dict[str, Any]:
    """Read YAML frontmatter from the Obsidian scoring file. Empty dict
    on missing file or unparseable YAML."""
    if not path or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("scoring: read %s failed: %s", path, exc)
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        parsed = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "scoring: YAML parse failed in %s: %s — falling back to defaults",
            path, exc,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_float_dict(raw: Any) -> dict[str, float]:
    """Filter an override section to {str: float}; warn on bad entries."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            logger.warning("scoring: ignoring non-numeric override %s=%r", k, v)
    return out


def _load_scoring_weights() -> dict[str, dict[str, float]]:
    """Defaults merged with user overrides from Obsidian. Cached on mtime.

    The merge is per-section shallow — user-supplied keys override the
    matching default key; defaults for unmentioned keys are preserved.
    Sections the user doesn't mention fall through to defaults entirely.
    """
    global _WEIGHTS_CACHE
    path = _scoring_weights_path()
    mtime = 0.0
    if path and path.exists():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
    cached = _WEIGHTS_CACHE
    if cached and cached[0] == path and cached[1] == mtime:
        return cached[2]
    overrides = _read_overrides(path)
    merged: dict[str, dict[str, float]] = {}
    for section, defaults in _DEFAULT_WEIGHTS.items():
        merged[section] = {**defaults, **_coerce_float_dict(overrides.get(section))}
    _WEIGHTS_CACHE = (path, mtime, merged)
    if overrides:
        logger.info(
            "scoring: weights loaded from %s (mtime=%.0f, sections=%s)",
            path, mtime, sorted(overrides.keys()),
        )
    return merged


def tier_thresholds() -> tuple[float, float]:
    """(high, medium) score cutoffs — user-tunable via Scoring Weights.md."""
    section = _load_scoring_weights().get("signal_tiers", SIGNAL_TIER_DEFAULTS)
    return (
        section.get("high",   SIGNAL_TIER_DEFAULTS["high"]),
        section.get("medium", SIGNAL_TIER_DEFAULTS["medium"]),
    )


def tier_for_score(
    score: float | None,
    high: float | None = None,
    medium: float | None = None,
) -> str | None:
    """Map a leaderboard score to a conviction tier. None score → None tier.

    Thresholds default to the user-tuned `signal_tiers` weights when omitted.
    `score_item` passes the batch thresholds in so the tier it persists matches
    the score it was computed alongside.
    """
    if score is None:
        return None
    if high is None or medium is None:
        wh, wm = tier_thresholds()
        high = wh if high is None else high
        medium = wm if medium is None else medium
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"


def tier_badge(score: float | None) -> str:
    """'🔴 High' / '🟡 Medium' / '🔵 Low' for a score; '' when score is None."""
    tier = tier_for_score(score)
    return f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]}" if tier else ""


# ── TPLF / litigation-financing first-class boost (Wave 3 Phase 2) ────
#
# Fires when EITHER the LLM-classified sub_tags list contains
# 'litigation_tplf' OR the title/summary regex hits one of the explicit
# TPLF / mass-tort phrasings. Stacks on top of topic_priority_boost for
# social_inflation (1.4×) — a TPLF item lands at 1.4 × 1.3 = 1.82× topic
# weight, which is the user's intent given the Liability Intelligence
# cluster scope in Wave 3.

LITIGATION_TPLF_BOOST = 1.3

_TPLF_KEYWORDS = (
    r"\bthird[- ]party (?:litigation )?(?:financ\w+|fund\w+)\b",
    r"\bTPLF\b",
    r"\blitigation (?:funder|financier|finance|funding)\b",
    r"\battorney (?:advance|funding)\b",
    r"\bjudgment monetization\b",
    r"\bmass tort (?:funder|MDL)\b",
    r"\bnuclear verdict\b",
    r"\baggregate (?:settlement|verdict)\b",
    r"\bMDL panel\b",
)

_TPLF_RE = re.compile("|".join(_TPLF_KEYWORDS), re.IGNORECASE)


def _text_blob(row: Any) -> str:
    """Concatenate title + summary + why_it_matters for keyword scanning."""
    parts: list[str] = []
    for key in ("title", "summary", "why_it_matters"):
        if key in row.keys() and row[key]:
            parts.append(str(row[key]))
    return " ".join(parts)


def _litigation_tplf_boost(row: Any, blob: str, boost_value: float = 1.3) -> float:
    """Returns `boost_value` when the LLM tagged the item with
    litigation_tplf sub_tag OR the title/summary names a TPLF / MDL signal.
    Pass the pre-computed `blob` so score_item doesn't rebuild it per helper.
    """
    sub_tags_json = row["sub_tags"] if "sub_tags" in row.keys() else None
    if sub_tags_json:
        try:
            tags = json.loads(sub_tags_json)
            if "litigation_tplf" in tags:
                return boost_value
        except (TypeError, ValueError):
            pass
    return boost_value if blob and _TPLF_RE.search(blob) else 1.0


def _regulatory_action_boost(blob: str, boost_value: float = 1.2) -> float:
    """Returns `boost_value` if the row blob names a state DOI action,
    insurer of last resort, or SERFF rate filing.
    """
    return boost_value if blob and _REGULATORY_RE.search(blob) else 1.0


def _inflation_keyword_boost(blob: str, boost_value: float = 1.2) -> float:
    """Returns `boost_value` if the row blob names an inflation driver."""
    return boost_value if blob and _INFLATION_RE.search(blob) else 1.0


def _insurer_priority_boost(
    source: str,
    metadata_json: Any,
    insurer_map: dict[str, float] | None = None,
) -> float:
    """Returns per-ticker boost for EDGAR items; 1.0 otherwise.

    `insurer_map` defaults to the module-level PRIORITY_INSURERS_BOOST so
    older external callers (tests, scripts) continue to work; production
    callers pass the user-overridable map from _load_scoring_weights().
    """
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
    table = insurer_map if insurer_map is not None else PRIORITY_INSURERS_BOOST
    return table.get(ticker, 1.0)


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
    tier: str
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
    tplf_boost: float

    def as_row(self, computed_at: str) -> dict[str, Any]:
        return {
            "item_id":          self.item_id,
            "computed_at":      computed_at,
            "score":            self.score,
            "tier":             self.tier,
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
            "tplf_boost":       self.tplf_boost,
        }


def score_item(
    row: Any,
    regime: RegimeSignal,
    weights: dict[str, dict[str, float]] | None = None,
) -> Score:
    """Compute the leaderboard score for one item row.

    Weights resolution order: explicit `weights` arg → cached load from
    Obsidian _meta/Scoring Weights.md → module-level defaults. Production
    `run_signals` passes a single resolved dict so a per-batch reload
    happens at most once; ad-hoc callers can omit and pay the cached lookup.
    """
    if weights is None:
        weights = _load_scoring_weights()

    source     = (row["source"] or "").lower()
    topic      = (row["topic"]  or "").lower()

    sources    = weights["sources"]
    src_mult   = sources.get(source, sources.get("default", SOURCE_MULT_DEFAULT))
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

    topic_boost  = weights["topics"].get(topic, 1.0)

    burden_intensity = (
        row["burden_intensity"] if "burden_intensity" in row.keys() else None
    )
    burden_boost = weights["burden_intensity"].get(
        (burden_intensity or "").lower(), 1.0
    )

    kw = weights["keyword_boosts"]
    metadata_json = row["metadata_json"] if "metadata_json" in row.keys() else None
    blob = _text_blob(row)   # build once, reuse across the three keyword helpers
    insurer_boost    = _insurer_priority_boost(source, metadata_json, weights["insurer_priority"])
    inflation_boost  = _inflation_keyword_boost(blob,      kw.get("inflation",  1.2))
    regulatory_boost = _regulatory_action_boost(blob,      kw.get("regulatory", 1.2))
    tplf_boost       = _litigation_tplf_boost(row, blob,   kw.get("tplf",       1.3))

    score = (
        src_mult * rg_mult * topic_rel * rec
        * llm_j * topic_boost * burden_boost
        * insurer_boost * inflation_boost * regulatory_boost
        * tplf_boost
    )
    st = weights["signal_tiers"]
    tier = tier_for_score(score, st.get("high"), st.get("medium"))
    return Score(
        item_id=int(row["id"]),
        score=round(score, 4),
        tier=tier,
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
        tplf_boost=round(tplf_boost, 3),
    )


# ── Public API ────────────────────────────────────────────────────────


def run_signals() -> dict[str, int]:
    """Score every kept+summarized item against the current regime; persist."""
    regime = current_regime()
    rows   = db.items_for_signals()
    if not rows:
        logger.info("signals: nothing to score")
        return {"scored": 0}

    # Resolve weights once per batch so the YAML mtime stat happens at most
    # once even when scoring hundreds of items. Edits to Scoring Weights.md
    # land on the next run, not mid-batch.
    weights = _load_scoring_weights()
    computed_at = datetime.now(timezone.utc).isoformat()
    scored: list[dict[str, Any]] = []
    for row in rows:
        s = score_item(row, regime, weights=weights)
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
