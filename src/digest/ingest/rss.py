"""RSS ingestor — P&C trade press + Google News proxies (config/rss_feeds.yaml).

Fetch mechanics live in `digest_core.ingest.rss.fetch_feeds`; this shell owns
the PC feed list and the `store=db` binding (via digest.ingest.base).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.rss import fetch_feeds

RSS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "rss_feeds.yaml"


class RSSIngestor(IngestorBase):
    name = "rss"
    enrich_fulltext = True  # feeds carry excerpts, not full articles

    def __init__(self) -> None:
        self.feeds = yaml.safe_load(RSS_CONFIG.read_text())["feeds"]

    def fetch(self) -> list[IngestedItem]:
        return fetch_feeds(self.feeds, self.name, default_limit=15)
