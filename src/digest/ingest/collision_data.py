"""Collision data ingestor (CCC Intelligent Solutions + Mitchell/Enlyte) — SCAFFOLD.

Quarterly auto-physical-damage industry reports from the two dominant
claims-tech vendors. Fires `supply_chain` topic boost (1.4) +
`inflation_keyword_boost` on severity/repair-cost keywords. High signal,
low volume (~6-10 reports/year combined).

Status: scaffold only. fetch() returns [] with a TODO log line until the
HTML scrape patterns are confirmed (publishers' news pages can change
between quarterly cycles).

Sources:
  - CCC Intelligent Solutions news & events
    https://www.cccis.com/about-us/news-and-events/
    Look for entries titled "Q1/Q2/Q3/Q4 ... Crash Course" or
    "Industry Trends Report".

  - Mitchell / Enlyte news
    https://www.mitchell.com/news
    Look for entries titled "Mitchell Industry Trends Report"
    or "Auto Physical Damage Edition".

Target topic: supply_chain (locked via metadata.topic_hint).
"""
from __future__ import annotations

import logging

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

# When implementing: keep a published-id cache (sqlite of seen URLs)
# so we don't re-emit the same quarterly report twice. CCC + Mitchell
# both publish on news pages with stable per-article URLs.

CCC_NEWS_URL     = "https://www.cccis.com/about-us/news-and-events/"
MITCHELL_NEWS_URL = "https://www.mitchell.com/news"

# Title-substring filters for relevance (only emit on these):
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


class CollisionDataIngestor(IngestorBase):
    name = "collision"

    def fetch(self) -> list[IngestedItem]:
        # TODO(Wave 3): implement HTML scrapes for CCC + Mitchell.
        # Sketch:
        #
        #   from bs4 import BeautifulSoup
        #   items: list[IngestedItem] = []
        #
        #   for vendor, url, filters in (
        #       ("ccc",      CCC_NEWS_URL,      CCC_TITLE_FILTERS),
        #       ("mitchell", MITCHELL_NEWS_URL, MITCHELL_TITLE_FILTERS),
        #   ):
        #       r = requests.get(url, headers={"User-Agent": "..."}, timeout=20)
        #       r.raise_for_status()
        #       soup = BeautifulSoup(r.text, "html.parser")
        #       for card in soup.select(".news-card, .post-item, article"):  # tune per vendor
        #           title = (card.select_one("h2,h3,.title") or {}).get_text(strip=True)
        #           if not any(f in title.lower() for f in filters):
        #               continue
        #           href = (card.select_one("a") or {}).get("href")
        #           date_str = (card.select_one("time,.date") or {}).get_text(strip=True)
        #           items.append(IngestedItem(
        #               source="collision",
        #               source_id=f"{vendor}:{href}",
        #               title=f"{vendor.upper()}: {title}",
        #               url=href,
        #               author=vendor.upper(),
        #               published_at=_parse(date_str),
        #               metadata={
        #                   "topic_hint": "supply_chain",
        #                   "vendor":     vendor,
        #               },
        #           ))
        logger.info("collision: TODO — HTML scrape not implemented yet")
        return []
