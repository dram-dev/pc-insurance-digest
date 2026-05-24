"""NHC ingestor — NOAA/NHC public advisory RSS for active Atlantic/Pacific storms."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from time import mktime

import feedparser

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

# NHC publishes one RSS feed per storm slot. Slots 1-5 for Atlantic (at),
# 1-3 for East Pacific (ep), 1 for Central Pacific (cp).
_FEEDS = (
    [f"https://www.nhc.noaa.gov/nhc_at{i}.xml" for i in range(1, 6)]
    + [f"https://www.nhc.noaa.gov/nhc_ep{i}.xml" for i in range(1, 4)]
    + ["https://www.nhc.noaa.gov/nhc_cp1.xml"]
)

# Phrases that indicate U.S. or Caribbean insurance exposure.
_THREAT_TOKENS = frozenset(
    [
        "united states", "gulf of mexico", "gulf coast", "caribbean",
        "florida", "texas", "louisiana", "georgia", "south carolina",
        "north carolina", "virginia", "puerto rico", "u.s. virgin islands",
        "bahamas", "cuba", "mexico", "belize", "haiti",
        "hurricane warning", "hurricane watch",
        "tropical storm warning", "tropical storm watch",
        "landfall", "making landfall",
    ]
)


def _has_us_threat(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(tok in combined for tok in _THREAT_TOKENS)


class NHCIngestor(IngestorBase):
    name = "nhc"

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for url in _FEEDS:
            try:
                parsed = feedparser.parse(url)
                if parsed.bozo:
                    logger.debug("nhc: %s bozo=%s", url, parsed.bozo_exception)
                for entry in parsed.entries:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    # Empty storm slots emit a placeholder — skip them.
                    tl = title.lower()
                    if not title or "there are no" in tl or "no current storm" in tl or "no tropical" in tl:
                        continue
                    if not _has_us_threat(title, summary):
                        continue
                    raw_id = entry.get("id") or entry.get("link") or title
                    source_id = hashlib.sha1(raw_id.encode()).hexdigest()[:16]
                    pub: datetime | None = None
                    for key in ("published_parsed", "updated_parsed"):
                        st = entry.get(key)
                        if st:
                            pub = datetime.fromtimestamp(mktime(st))
                            break
                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=source_id,
                            title=title,
                            url=entry.get("link"),
                            content=summary,
                            published_at=pub,
                            metadata={"feed_url": url, "topic_hint": "cat_event"},
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("nhc: failed on %s: %s", url, exc)
        return items
