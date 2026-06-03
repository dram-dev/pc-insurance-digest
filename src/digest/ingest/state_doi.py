"""State Department of Insurance direct-scraper ingestor.

Scrapes top-5 P&C state DOI press release pages directly, complementing
the Google News proxies in config/rss_feeds.yaml (faster bulletin pickup;
no Google indexing lag).

Each state in config/state_doi_sources.yaml has `enabled: false`. Flip to
true once the CSS selector is confirmed against the live page. Use:
  curl -sL -A 'Mozilla/5.0' <url> | grep -A5 'press\\|release\\|news'
to discover the correct selector, then update state_doi_sources.yaml.

Target topic: regulatory_rate (locked by auto_keep_state_doi hook).
Fires regulatory_action_boost (1.2×) + regulatory_rate topic boost (1.2×).

Build order (highest volume first): CA → FL → TX → NY → LA.

Current selector status (all TODO — validate with curl on Mac mini):
  CA: .releases-list .release-item — CDI uses a Drupal-based CMS; likely correct
  FL: .news-item — FLOIR; unverified
  TX: .news-listing li — TDI; unverified
  NY: .pr-listing-item — DFS; unverified
  LA: .views-row — LDI Drupal; plausible
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.dates import parse_date

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "state_doi_sources.yaml"
_REQUEST_TIMEOUT = 20
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class StateDOIIngestor(IngestorBase):
    name = "state_doi"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"state_doi config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        year = datetime.now(tz=timezone.utc).year
        enabled_count = 0
        for entry in self.config.get("states", []):
            if not entry.get("enabled", False):
                continue
            enabled_count += 1
            state  = entry["state"]
            agency = entry["agency"]
            url    = entry["url_template"].format(year=year)
            try:
                items.extend(self._scrape_state(state, agency, url, entry))
            except Exception as exc:  # noqa: BLE001
                logger.warning("state_doi: %s scrape failed: %s", state, exc)

        if enabled_count == 0:
            logger.info(
                "state_doi: no states enabled — flip enabled:true in config/state_doi_sources.yaml "
                "after validating the CSS selector for each state"
            )
        return items

    def _scrape_state(
        self,
        state: str,
        agency: str,
        url: str,
        entry: dict,
    ) -> list[IngestedItem]:
        # JS-rendered or WAF-blocked states (e.g. TX year index, LA behind a
        # WAF) set `render: true` to fetch the final DOM via a headless browser;
        # everything else takes the plain requests path. A missing render extra
        # returns None → skip this state (logged), don't crash the run.
        if entry.get("render"):
            from digest.ingest.render import fetch_rendered
            html = fetch_rendered(url, wait_selector=entry.get("selector") or None)
            if html is None:
                logger.warning(
                    "state_doi: %s — rendered fetch unavailable (install the render "
                    "extra: `uv sync --extra render && uv run playwright install "
                    "chromium`); skipping", state,
                )
                return []
        else:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=_REQUEST_TIMEOUT)
            r.raise_for_status()
            html = r.text
        soup = BeautifulSoup(html, "html.parser")

        selector = entry.get("selector", "")
        nodes = soup.select(selector) if selector else []
        if not nodes:
            logger.warning(
                "state_doi: %s — selector %r returned 0 nodes; "
                "page structure may have changed. Disable this state until selector is fixed.",
                state, selector,
            )
            return []

        # `max_items` caps newest-first listings that render their whole archive
        # (LA LDI lists ~97 releases back to 2024) — keep only the most recent.
        max_items = entry.get("max_items")
        if max_items:
            nodes = nodes[: int(max_items)]

        items: list[IngestedItem] = []
        seen_urls: set[str] = set()
        for node in nodes:
            # Title: `title_from_node` takes the row's own text when the headline
            # isn't a child element (LA LDI: each release is a <p> whose text IS
            # the headline, its only anchor being the date link). Otherwise a
            # per-state `title_selector` override (FL FLOIR uses
            # span.newsSummary.h-3), falling back to common heading/anchor patterns.
            if entry.get("title_from_node"):
                title = node.get_text(" ", strip=True)[:200]
            else:
                title_sel = entry.get("title_selector") or "a, h2, h3, h4, .title, .headline"
                title_el = node.select_one(title_sel)
                title = title_el.get_text(strip=True) if title_el else node.get_text(strip=True)[:200]
            if not title:
                continue

            # Href: prefer first anchor
            a = node.select_one("a[href]")
            href = a.get("href", "") if a else ""
            if href and not href.startswith("http"):
                href = urljoin(url, href)
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            # Date: a per-state `date_selector` override (sites name the date
            # element idiosyncratically — CA CDI uses span.secondaryHeader),
            # falling back to the common patterns.
            date_sel = entry.get("date_selector") or (
                "time, .date, .release-date, [class*='date'], span[class*='time']"
            )
            date_el = node.select_one(date_sel)
            date_text = (date_el.get("datetime") or date_el.get_text(strip=True)) if date_el else ""
            pub = parse_date(date_text)
            if pub is None:
                # Many newsrooms encode the date only in the URL path (FL FLOIR:
                # /newsroom/archives/item-details/2026/05/20/slug). Fall back to a
                # YYYY/MM/DD found in the href so items aren't left undated.
                m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)", href)
                if m:
                    y, mo, d = (int(g) for g in m.groups())
                    pub = parse_date(f"{y:04d}-{mo:02d}-{d:02d}")

            source_id = f"{state}:{urlparse(href).path}"
            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=source_id,
                    title=f"[{state} DOI] {title}",
                    url=href,
                    author=agency,
                    published_at=pub,
                    metadata={
                        "topic_hint": "regulatory_rate",
                        "state":      state,
                        "agency":     agency,
                    },
                )
            )

        logger.info("state_doi: %s — %d items scraped", state, len(items))
        return items
