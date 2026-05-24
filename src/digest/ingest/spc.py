"""SPC ingestor — Storm Prediction Center watches and warnings via RSS."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from time import mktime

import feedparser

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_WATCH_RSS = "https://www.spc.noaa.gov/products/spcwwrss.xml"
_FEEDS = [_WATCH_RSS]
_ITEM_LIMIT = 25

# Only pass items with meaningful P&C CAT exposure through to triage.
_SIGNAL_TOKENS = frozenset(
    [
        "tornado", "severe thunderstorm", "enhanced", "moderate risk",
        "high risk", "particularly dangerous situation", "pds",
        "convective outlook",
    ]
)


def _is_relevant(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(tok in combined for tok in _SIGNAL_TOKENS)


class SPCIngestor(IngestorBase):
    name = "spc"

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for url in _FEEDS:
            try:
                parsed = feedparser.parse(url)
                if parsed.bozo:
                    logger.debug("spc: %s bozo=%s", url, parsed.bozo_exception)
                for entry in parsed.entries[:_ITEM_LIMIT]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    if not _is_relevant(title, summary):
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
                logger.warning("spc: failed on %s: %s", url, exc)
        return items
