"""Generic Hacker News fetching via the Algolia search API (free, no auth).

`fetch_hn` runs a list of domain-supplied keyword queries and returns deduped
story IngestedItems above a points threshold. The query list + threshold are
the domain's (PC tracks different terms than macro); the fetch/dedup/mapping
mechanics live here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"


def fetch_hn(
    queries: list[str],
    min_points: int,
    hits_per_query: int = 10,
    source_name: str = "hn",
    timeout_sec: int = 15,
) -> list[IngestedItem]:
    """Search HN for each query, dedup by story id, map to IngestedItems.

    Per-query failures are logged and skipped. Stories seen under an earlier
    query are not re-emitted.
    """
    items: list[IngestedItem] = []
    seen_ids: set[str] = set()
    for q in queries:
        try:
            r = requests.get(
                ALGOLIA_URL,
                params={
                    "query": q,
                    "tags": "story",
                    "hitsPerPage": hits_per_query,
                    "numericFilters": f"points>={min_points}",
                },
                timeout=timeout_sec,
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
                        published = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        published = None
                items.append(
                    IngestedItem(
                        source=source_name,
                        source_id=hid,
                        title=hit.get("title") or "(no title)",
                        url=hit.get("url") or f"https://news.ycombinator.com/item?id={hid}",
                        author=hit.get("author"),
                        content=hit.get("story_text") or "",
                        published_at=published or datetime.now(timezone.utc),
                        metadata={
                            "points": hit.get("points"),
                            "num_comments": hit.get("num_comments"),
                            "query": q,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: failed query '%s': %s", source_name, q, exc)
    return items
