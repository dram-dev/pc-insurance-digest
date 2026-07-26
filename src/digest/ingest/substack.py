"""Substack / newsletter ingestor (config/substack_feeds.yaml).

A thin domain shell over the shared `digest_core.ingest.rss.fetch_feeds` — same
mechanics as the RSS ingestor, different feed list, `source`, and per-feed
default limit.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.rss import fetch_feeds

SUBSTACK_CONFIG = Path(__file__).resolve().parents[3] / "config" / "substack_feeds.yaml"


class SubstackIngestor(IngestorBase):
    name = "substack"
    enrich_fulltext = True  # feeds carry excerpts, not full articles

    def __init__(self) -> None:
        self.feeds = yaml.safe_load(SUBSTACK_CONFIG.read_text())["feeds"]

    def fetch(self) -> list[IngestedItem]:
        return fetch_feeds(self.feeds, self.name, default_limit=10)
