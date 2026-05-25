"""Industry research ingestor — LexisNexis Risk Solutions + JD Power (Phase 2).

Phase 1 coverage is the `google_news_industry_research` RSS proxy in
config/rss_feeds.yaml. This ingestor scrapes the vendors' own publication
pages directly for auto-insurance content that Google News may miss or
delay.

Config lives in config/industry_research_sources.yaml (per-source enabled
flag). All sources start disabled; flip enabled:true after validating the
CSS selector against the live page.

Auto-keep is handled by db.auto_keep_quantitative() — 'industry_research'
is in QUANT_SOURCES. topic_hint = 'personal_lines' is passed via metadata.

TODO: validate selectors before enabling either source. In the remote
execution environment (cloud) outbound network is restricted; validate
on the Mac mini with:
  curl -sL -A 'Mozilla/5.0' <url> | grep -A3 'article\\|press\\|release'
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "industry_research_sources.yaml"
_REQUEST_TIMEOUT = 20
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _parse_date(text: str) -> datetime | None:
    clean = (text or "").strip()
    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _scrape_source(entry: dict) -> list[IngestedItem]:
    name         = entry["name"]
    vendor       = entry["vendor"]
    url          = entry["url"]
    title_filter = entry.get("title_filter", "").lower()
    selector     = entry.get("selector", "")

    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("industry_research: %s fetch failed: %s", name, exc)
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    nodes = soup.select(selector) if selector else []
    if not nodes:
        logger.warning(
            "industry_research: %s — selector %r returned 0 nodes; "
            "TODO: validate selector against %s",
            name, selector, url,
        )
        return []

    items: list[IngestedItem] = []
    seen_urls: set[str] = set()
    for node in nodes:
        title_el = node.select_one("h1, h2, h3, h4, .title, .headline, a")
        title = title_el.get_text(strip=True) if title_el else node.get_text(strip=True)[:200]
        if not title:
            continue
        if title_filter and title_filter not in title.lower():
            continue

        a = node.select_one("a[href]")
        href = a.get("href", "") if a else ""
        if href and not href.startswith("http"):
            href = urljoin(url, href)
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        date_el   = node.select_one("time, .date, [class*='date']")
        date_text = (date_el.get("datetime") or date_el.get_text(strip=True)) if date_el else ""
        pub       = _parse_date(date_text)

        source_id = f"{name}:{urlparse(href).path}"
        items.append(
            IngestedItem(
                source="industry_research",
                source_id=source_id,
                title=f"[{vendor}] {title}",
                url=href,
                author=vendor,
                published_at=pub,
                metadata={
                    "topic_hint": "personal_lines",
                    "vendor":     vendor,
                    "source_name": name,
                },
            )
        )

    logger.info("industry_research: %s — %d items", name, len(items))
    return items


class IndustryResearchIngestor(IngestorBase):
    name = "industry_research"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"industry_research config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        enabled = [s for s in self.config.get("sources", []) if s.get("enabled", False)]
        if not enabled:
            logger.info(
                "industry_research: no sources enabled — flip enabled:true in "
                "config/industry_research_sources.yaml after validating selectors"
            )
            return []
        for source in enabled:
            items.extend(_scrape_source(source))
        return items
