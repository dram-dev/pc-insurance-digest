"""Hacker News ingestor — Algolia search over P&C-relevant keyword queries.

Fetch mechanics live in `digest_core.ingest.hackernews.fetch_hn`; this shell
owns the domain query list + points threshold.

NOTE: QUERIES below are still the macro-ai-digest AI/semis terms carried over
from the copy-modify origin — they should be retuned to P&C insurtech / cyber
/ catastrophe terms. Tracked as a separate config fix.
"""
from __future__ import annotations

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.hackernews import fetch_hn

QUERIES = [
    "LLM",
    "AI capex",
    "hyperscaler",
    "datacenter",
    "semiconductor",
    "GPU",
    "inference",
    "Anthropic",
    "OpenAI",
    "Karpathy",
]

HITS_PER_QUERY = 10


class HNIngestor(IngestorBase):
    name = "hn"
    enrich_fulltext = True  # feeds carry excerpts, not full articles

    def fetch(self) -> list[IngestedItem]:
        return fetch_hn(
            QUERIES,
            min_points=settings.hn_min_points,
            hits_per_query=HITS_PER_QUERY,
            source_name=self.name,
        )
