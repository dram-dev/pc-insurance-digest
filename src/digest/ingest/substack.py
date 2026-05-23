"""Substack / newsletter ingestor — reads configured publication feeds via RSS/Atom."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from time import mktime

import feedparser
import yaml

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

SUBSTACK_CONFIG = Path(__file__).resolve().parents[3] / "config" / "substack_feeds.yaml"


def _entry_id(entry: dict) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _entry_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(mktime(st))
    return None


def _entry_content(entry: dict) -> str:
    if "content" in entry and entry["content"]:
        return entry["content"][0].get("value", "")
    return entry.get("summary", "")


class SubstackIngestor(IngestorBase):
    name = "substack"

    def __init__(self) -> None:
        self.feeds = yaml.safe_load(SUBSTACK_CONFIG.read_text())["feeds"]

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for feed_cfg in self.feeds:
            url = feed_cfg["url"]
            label = feed_cfg.get("name", url)
            topic_hint = feed_cfg.get("topic_hint")
            try:
                parsed = feedparser.parse(url)
                if parsed.bozo:
                    logger.warning("substack: %s bozo=%s", label, parsed.bozo_exception)
                for entry in parsed.entries[: feed_cfg.get("limit", 10)]:
                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=f"{label}:{_entry_id(entry)}",
                            title=entry.get("title", "(no title)"),
                            url=entry.get("link"),
                            author=entry.get("author"),
                            content=_entry_content(entry),
                            published_at=_entry_date(entry),
                            metadata={
                                "feed": label,
                                "feed_url": url,
                                "topic_hint": topic_hint,
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("substack: failed on %s: %s", label, exc)
        return items
