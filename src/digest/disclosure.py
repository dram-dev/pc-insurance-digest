"""Disclosure-sentiment NLP (EKG Lead 5) — reserve-tone read over EDGAR filings.

A *language* signal on reserve adequacy that **leads** the chain-ladder number:
reserve tone in earnings commentary / MD&A often softens before a loss-
development triangle confirms adverse development. Scores the EDGAR filing text
already stored on items (8-K EX-99.1 earnings releases carry the prior-year
reserve-development commentary; 10-Q/10-K head) with a compact, deterministic
reserve-tone lexicon, and feeds the *same* boost as Lead 6:

    EDGAR items.content
      → disclosure.score_filing()   → (reserve_tone, adverse_language_score)
      → db.upsert_disclosure_sentiment()
      → db.reserving_severity_map()  (blends language severity ∪ chain-ladder)
      → signals._reserve_deterioration_boost()  → leaderboard

Adverse tone can fire the boost on its own, but only up to 1 + LANG_SEVERITY_CAP
(= 1.15) — strictly below a confirmed chain-ladder triangle's 1.30 cap — so the
actuarial number stays authoritative while tone gives the early read.

Databricks-native upgrade: `ai_query()` (LLM tone classification in SQL) or a
FinBERT / full Loughran-McDonald lexicon over EDGAR body text → silver.
disclosure_sentiment. This local lexicon is the Free-Edition / CPU-only default;
the downstream boost wiring is identical regardless of which tone engine produced
the score.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from digest import db
from digest.outcomes import match_insurer

logger = logging.getLogger(__name__)

# Language-only severity ceiling: an insurer flagged adverse by TONE ALONE
# contributes at most this to the reserve severity map → boost ≤ 1 + this = 1.15,
# below a confirmed chain-ladder triangle's 1.30 cap (reserving.RESERVE_BOOST_CAP).
# Keeps the actuarial number authoritative while letting tone lead it.
LANG_SEVERITY_CAP = 0.15

# Compact reserve-tone lexicon, matched case-insensitively. ADVERSE = reserves
# moving UP (strengthening / deficiency); FAVORABLE = reserves moving DOWN
# (releases / redundancy). Phrases are reserve-specific so a generic
# "increase"/"decrease" elsewhere in the filing doesn't fire.
_ADVERSE = [
    r"reserve\s+strengthening",
    r"strengthen(?:ed|ing)?\s+(?:its\s+|the\s+)?(?:loss\s+)?reserves",
    r"(?:unfavorable|adverse)\s+(?:prior[\s-]*year\s+)?(?:reserve\s+|loss\s+)?development",
    r"reserve\s+(?:deficienc(?:y|ies)|charges?|increases?|additions?)",
    r"(?:increased|raised|added\s+to|bolstered|boosted)\s+(?:its\s+|the\s+)?(?:loss\s+)?reserves",
    r"prior[\s-]*year\s+reserve\s+(?:increase|strengthening)",
    r"under[\s-]*reserv",
    r"deficienc(?:y|ies)",
]
_FAVORABLE = [
    r"favorable\s+(?:prior[\s-]*year\s+)?(?:reserve\s+|loss\s+)?development",
    r"reserve\s+releases?",
    r"released?\s+(?:its\s+|the\s+)?(?:loss\s+)?reserves",
    r"(?:reduced|lowered)\s+(?:its\s+|the\s+)?(?:loss\s+)?reserves",
    r"reserve\s+(?:redundanc(?:y|ies)|reductions?)",
    r"redundan(?:t|cy)",
    r"prior[\s-]*year\s+favorable\s+development",
]
_ADVERSE_RE = [re.compile(p, re.IGNORECASE) for p in _ADVERSE]
_FAVORABLE_RE = [re.compile(p, re.IGNORECASE) for p in _FAVORABLE]


def score_filing(text: str) -> tuple[str, float]:
    """Reserve tone of a filing's text → (reserve_tone, adverse_language_score).

    reserve_tone ∈ {'strengthening','releasing','neutral'}; adverse_language_score
    ∈ [0,1], where 0 means no reserve discussion (or favorable-dominant tone) and
    higher means more adverse framing. Deterministic — the same text always yields
    the same read.
    """
    if not text:
        return "neutral", 0.0
    a = sum(len(rx.findall(text)) for rx in _ADVERSE_RE)
    f = sum(len(rx.findall(text)) for rx in _FAVORABLE_RE)
    if a == 0 and f == 0:
        return "neutral", 0.0
    tone = "strengthening" if a > f else "releasing" if f > a else "neutral"
    score = max(0.0, (a - f) / (a + f))
    return tone, round(score, 4)


def language_severity(reserve_tone: str | None, adverse_language_score: float) -> float:
    """Language-derived reserve severity for the boost map: scaled to ≤
    LANG_SEVERITY_CAP, and only for adverse ('strengthening') tone, else 0.0."""
    if reserve_tone != "strengthening" or adverse_language_score <= 0:
        return 0.0
    return round(adverse_language_score * LANG_SEVERITY_CAP, 4)


def _period(filing_date: str) -> str:
    """'YYYYQn' from an ISO 'YYYY-MM-DD…' date; '' when unparseable."""
    try:
        d = datetime.strptime(filing_date[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def run_disclosure() -> dict[str, int]:
    """Score reserve tone over stored EDGAR filings → disclosure_sentiment.

    Reads EDGAR items that carry body content, attributes each to an insurer
    ticker (filing metadata, else a name match on title/author), scores the text,
    and upserts one disclosure_sentiment row per (insurer, period, as_of).
    """
    rows = db.edgar_filings_with_content()
    scored = 0
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        ticker = meta.get("ticker") or match_insurer(f"{r['title']} {r['author'] or ''}")
        as_of = (r["published_at"] or r["ingested_at"] or "")[:10]
        if not ticker or not as_of:
            continue
        tone, score = score_filing(r["content"])
        db.upsert_disclosure_sentiment({
            "insurer": ticker,
            "period": _period(as_of) or as_of,
            "as_of": as_of,
            "reserve_tone": tone,
            "adverse_language_score": score,
            "source_filing": meta.get("accession") or r["url"],
        })
        scored += 1
    logger.info("disclosure: scored %d/%d EDGAR filings", scored, len(rows))
    return {"filings": len(rows), "scored": scored}
