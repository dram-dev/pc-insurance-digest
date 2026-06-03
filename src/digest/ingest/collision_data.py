"""Collision data ingestor — CCC Intelligent Solutions + Mitchell/Enlyte.

Quarterly auto-physical-damage industry reports from the two dominant
claims-tech vendors. Fires `supply_chain` topic boost (1.4×) +
`inflation_keyword_boost` on severity/repair-cost keywords. High signal,
low volume (~6–10 reports/year combined).

Auto-keep is handled by `db.auto_keep_quantitative()` since 'collision'
is in QUANT_SOURCES. topic_hint = 'supply_chain' is passed via metadata.

Sources (validated live 2026-06-02 — both static HTML, no render needed):
  - CCC Intelligent Solutions: https://www.cccis.com/news-and-insights/news
      Webflow collection; each post card directly wraps its /posts/ link
      (`div:has(> a[href*='/news-and-insights/posts/'])` also grabs the featured
      Crash Course card the plain .news-card grid omits). CCC posts carry no
      machine-readable date → published_at falls back to ingested_at.
  - Mitchell / Enlyte:         https://www.mitchell.com/about/news
      Drupal listing; each `.listing-item` is wrapped in a PARENT <a>, so
      `_extract_href` walks to the ancestor anchor.

Both feeds mix high-value severity/cost/claims reports with corporate PR;
`COLLISION_TITLE_FILTERS` keeps only loss-cost-signal titles.
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

CCC_NEWS_URL      = "https://www.cccis.com/news-and-insights/news"
MITCHELL_NEWS_URL = "https://www.mitchell.com/about/news"

# Shared signal filter — these vendor pages mix high-value severity/cost/claims
# reports (what we want) with corporate PR (CFO appointments, product launches).
# Keep an item only if its title names a loss-cost / claims-trend signal.
# Validated against live titles 2026-06-02: catches "Crash Course … Higher
# Severity", "EV Collision Claims Rise 14%", "Hybrid … Record High", "Envision
# Trends Report", "PartsTrader"; drops "Announces CFO", "Extends Relationship".
COLLISION_TITLE_FILTERS = (
    "crash course", "trends report", "industry trends", "envision",
    "severity", "repair", "physical damage", "collision claim", "casualty",
    "claim frequency", "claims rise", "claims hit", "record high",
    "electric vehicle", "hybrid", "parts", "labor", "total loss",
    "diminished value", "adas", "subrogation",
)

# Candidate CSS selectors tried in order; first one that returns nodes is used.
# Validated live 2026-06-02: CCC is a Webflow collection (.news-card); Mitchell
# is a Drupal listing (.listing-item, each wrapped in a parent <a>).
_CCC_SELECTORS = [
    # Each post card directly wraps its /posts/ link; this also catches the
    # "featured" Crash Course card that the plain .news-card grid omits.
    "div:has(> a[href*='/news-and-insights/posts/'])",
    ".news-card",
    "div.w-dyn-item",
    ".news-list article",
]
_MITCHELL_SELECTORS = [
    ".listing-item",
    ".news-listing .item",
    "article.post",
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
    # The link may be a child (CCC card), the node itself, or a wrapping ancestor
    # (Mitchell wraps each .listing-item in a parent <a>).
    a = node.select_one("a[href]")
    if a is None and getattr(node, "name", None) == "a" and node.get("href"):
        a = node
    if a is None:
        a = node.find_parent("a", href=True)
    if not a:
        return None
    href = a.get("href", "")
    if href.startswith("http"):
        return href
    return urljoin(base_url, href)


from digest.parse.dates import parse_date


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
            "TODO: curl %s and update _%s_SELECTORS in collision_data.py",
            vendor, url, vendor.upper(),
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
        pub = parse_date(date_text)

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
        items.extend(_scrape("ccc",      CCC_NEWS_URL,      _CCC_SELECTORS,      COLLISION_TITLE_FILTERS))
        items.extend(_scrape("mitchell", MITCHELL_NEWS_URL, _MITCHELL_SELECTORS, COLLISION_TITLE_FILTERS))
        if not items:
            logger.info(
                "collision: 0 items — selectors may need updating once validated against live pages"
            )
        return items
