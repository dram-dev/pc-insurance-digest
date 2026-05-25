"""Collision data ingestor — CCC Intelligent Solutions + Mitchell/Enlyte.

Quarterly auto-physical-damage industry reports from the two dominant
claims-tech vendors. Fires `supply_chain` topic boost (1.4×) +
`inflation_keyword_boost` on severity/repair-cost keywords. High signal,
low volume (~6–10 reports/year combined).

Auto-keep is handled by `db.auto_keep_quantitative()` since 'collision'
is in QUANT_SOURCES. topic_hint = 'supply_chain' is passed via metadata.

Sources:
  - CCC Intelligent Solutions: https://www.cccis.com/about-us/news-and-events/
  - Mitchell / Enlyte:         https://www.mitchell.com/news

TODO: Both pages require selector validation. Run:
  curl -sL -A 'Mozilla/5.0' <url> | grep -i 'crash\\|industry\\|trend\\|report'
to find the correct article/card selectors. Then update _CCC_SELECTORS /
_MITCHELL_SELECTORS accordingly. Until validated, both lists return [] and
log a warning — safe to run in production.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

CCC_NEWS_URL      = "https://www.cccis.com/about-us/news-and-events/"
MITCHELL_NEWS_URL = "https://www.mitchell.com/news"

CCC_TITLE_FILTERS = (
    "crash course",
    "industry trends",
    "intelligence report",
)
MITCHELL_TITLE_FILTERS = (
    "industry trends",
    "auto physical damage",
    "casualty edition",
)

# Candidate CSS selectors tried in order; first one that returns nodes is used.
# TODO: validate against live pages (curl blocked in remote execution env).
_CCC_SELECTORS = [
    ".news-list article",
    ".news-events-list .item",
    "article.news-card",
    ".newsroom-item",
    "ul.news-listing li",
    "div.news-item",
    "div[class*='news'] article",
]
_MITCHELL_SELECTORS = [
    "article.post",
    ".post-listing article",
    ".news-listing .item",
    "div.blog-post",
    ".press-release-item",
    "ul.press li",
    "div[class*='post']",
]


def _try_selectors(soup: BeautifulSoup, selectors: list[str]) -> list[Any]:
    """Return nodes from the first selector that yields at least one result."""
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            return nodes
    return []


def _extract_text(node: Any, selectors: list[str]) -> str:
    for sel in selectors:
        el = node.select_one(sel)
        if el:
            return el.get_text(strip=True)
    return node.get_text(strip=True)[:200]


def _extract_href(node: Any, base_url: str) -> str | None:
    a = node.select_one("a[href]")
    if not a:
        return None
    href = a.get("href", "")
    if href.startswith("http"):
        return href
    return urljoin(base_url, href)


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    clean = text.strip()
    for fmt in (
        "%B %d, %Y",    # January 15, 2026
        "%b %d, %Y",    # Jan 15, 2026
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _scrape(vendor: str, url: str, selectors: list[str], filters: tuple[str, ...]) -> list[IngestedItem]:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("collision: %s fetch failed: %s", vendor, exc)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    nodes = _try_selectors(soup, selectors)
    if not nodes:
        # Log a warning so the operator knows to update selectors.
        logger.warning(
            "collision: %s — no nodes matched any selector; page structure may have changed. "
            "TODO: curl %s and update _%.upper()_SELECTORS in collision_data.py",
            vendor, url, vendor,
        )
        return []

    items: list[IngestedItem] = []
    seen_urls: set[str] = set()
    for node in nodes:
        title = _extract_text(node, ["h2", "h3", "h4", ".title", ".headline", "a"])
        if not title or not any(f in title.lower() for f in filters):
            continue
        href = _extract_href(node, url)
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        date_text = _extract_text(node, ["time", ".date", ".post-date", ".entry-date", "span[class*='date']"])
        pub = _parse_date(date_text)

        # Use URL path as source_id — stable across re-fetches.
        source_id = f"{vendor}:{urlparse(href).path}"
        items.append(
            IngestedItem(
                source="collision",
                source_id=source_id,
                title=f"{vendor.upper()}: {title}",
                url=href,
                author=vendor.upper(),
                published_at=pub,
                metadata={
                    "topic_hint": "supply_chain",
                    "vendor":     vendor,
                },
            )
        )

    logger.info("collision: %s — %d matching items from %d nodes", vendor, len(items), len(nodes))
    return items


class CollisionDataIngestor(IngestorBase):
    name = "collision"

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        items.extend(_scrape("ccc",      CCC_NEWS_URL,      _CCC_SELECTORS,      CCC_TITLE_FILTERS))
        items.extend(_scrape("mitchell", MITCHELL_NEWS_URL, _MITCHELL_SELECTORS, MITCHELL_TITLE_FILTERS))
        if not items:
            logger.info(
                "collision: 0 items — selectors may need updating once validated against live pages"
            )
        return items
