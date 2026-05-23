"""Hacker News ingestor — Algolia search API, AI/semi/capex keywords only.

Algolia HN API is free and requires no auth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"

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

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        seen_ids: set[str] = set()
        for q in QUERIES:
            try:
                r = requests.get(
                    ALGOLIA_URL,
                    params={
                        "query": q,
                        "tags": "story",
                        "hitsPerPage": HITS_PER_QUERY,
                        "numericFilters": f"points>={settings.hn_min_points}",
                    },
                    timeout=15,
                )
                r.raise_for_status()
                for hit in r.json().get("hits", []):
                    hid = str(hit.get("objectID"))
                    if hid in seen_ids:
                        continue
                    seen_ids.add(hid)
                    created_at = hit.get("created_at")
                    published = None
                    if created_at:
                        try:
                            published = datetime.fromisoformat(
                                created_at.replace("Z", "+00:00")
                            )
                        except ValueError:
                            published = None
                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=hid,
                            title=hit.get("title") or "(no title)",
                            url=hit.get("url")
                            or f"https://news.ycombinator.com/item?id={hid}",
                            author=hit.get("author"),
                            content=hit.get("story_text") or "",
                            published_at=published
                            or datetime.now(timezone.utc),
                            metadata={
                                "points": hit.get("points"),
                                "num_comments": hit.get("num_comments"),
                                "query": q,
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hn: failed query '%s': %s", q, exc)
        return items
