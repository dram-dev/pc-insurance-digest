"""Generic RSS/Atom feed fetching.

`fetch_feeds` turns a list of feed configs into IngestedItems. It's the shared
mechanics behind any domain feed ingestor (PC's `rss` + `substack`, macro's
equivalents) — the domain owns the feed list (YAML) and the `source` name;
this owns the parsing, entry-id hashing, date coercion, and content fallback.

A feed config is a dict: ``{"url": str, "name"?: str, "topic_hint"?: str,
"limit"?: int}``.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from time import mktime
from typing import Any

import feedparser

from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)


def entry_id(entry: dict[str, Any]) -> str:
    """Stable 16-char id for a feed entry (id → link → title)."""
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def entry_date(entry: dict[str, Any]) -> datetime | None:
    """Published/updated time as a datetime, or None."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(mktime(st))
    return None


def entry_content(entry: dict[str, Any]) -> str:
    """Full content when present, else the summary."""
    if "content" in entry and entry["content"]:
        return entry["content"][0].get("value", "")
    return entry.get("summary", "")


def fetch_feeds(
    feeds: list[dict[str, Any]],
    source_name: str,
    default_limit: int = 15,
) -> list[IngestedItem]:
    """Parse `feeds` into IngestedItems tagged with `source_name`.

    Per-feed failures are logged and skipped so one dead feed can't sink the
    batch. `default_limit` caps entries per feed unless the feed overrides it.
    """
    items: list[IngestedItem] = []
    for feed_cfg in feeds:
        url = feed_cfg["url"]
        label = feed_cfg.get("name", url)
        topic_hint = feed_cfg.get("topic_hint")
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo:
                logger.warning("%s: %s bozo=%s", source_name, label, parsed.bozo_exception)
            for entry in parsed.entries[: feed_cfg.get("limit", default_limit)]:
                items.append(
                    IngestedItem(
                        source=source_name,
                        source_id=f"{label}:{entry_id(entry)}",
                        title=entry.get("title", "(no title)"),
                        url=entry.get("link"),
                        author=entry.get("author"),
                        content=entry_content(entry),
                        published_at=entry_date(entry),
                        metadata={
                            "feed": label,
                            "feed_url": url,
                            "topic_hint": topic_hint,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: failed on %s: %s", source_name, label, exc)
    return items
