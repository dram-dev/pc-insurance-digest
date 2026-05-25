"""State Department of Insurance direct-scraper ingestor — SCAFFOLD.

Wave 3 ingestor that scrapes top-5 P&C state DOI press release pages
directly, complementing the Google News proxies in `config/rss_feeds.yaml`
(faster bulletin pickup; no Google indexing lag).

Status: scaffold only. Each state in `config/state_doi_sources.yaml` has
`enabled: false`. Flip to true as the per-state scrape logic is validated.
The fetch loop iterates only over enabled states; safe to register and
run unconfigured.

Sources (verified URLs, selectors TBD on first implementation):
  - CA CDI:   https://www.insurance.ca.gov/0400-news/0100-press-releases/{year}/
  - FL FLOIR: https://floir.com/News
  - TX TDI:   https://www.tdi.texas.gov/news/
  - NY DFS:   https://www.dfs.ny.gov/reports_and_publications/press_releases
  - LA LDI:   https://www.ldi.la.gov/news

Recommended build order (highest volume first):
  CA → FL → TX → NY → LA

Target topic: regulatory_rate (locked at auto-keep with a Python hook
analogous to db.auto_keep_nhc_advisories). Fires
`regulatory_action_boost` (1.2x) plus regulatory_rate topic boost (1.2x).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "state_doi_sources.yaml"


class StateDOIIngestor(IngestorBase):
    name = "state_doi"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"state_doi config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        year = datetime.utcnow().year
        for entry in self.config.get("states", []):
            if not entry.get("enabled", False):
                continue
            state = entry["state"]
            agency = entry["agency"]
            url = entry["url_template"].format(year=year)

            try:
                items.extend(self._scrape_state(state, agency, url, entry))
            except Exception as exc:  # noqa: BLE001
                logger.warning("state_doi: %s scrape failed: %s", state, exc)

        if not items:
            logger.info("state_doi: 0 enabled states — TODO implement scrapers")
        return items

    def _scrape_state(
        self,
        state: str,
        agency: str,
        url: str,
        entry: dict,
    ) -> list[IngestedItem]:
        # TODO(Wave 3): implement per-state scraping. Sketch:
        #
        #   from bs4 import BeautifulSoup
        #   r = requests.get(url, headers={"User-Agent": "..."}, timeout=20)
        #   r.raise_for_status()
        #   soup = BeautifulSoup(r.text, "html.parser")
        #   items: list[IngestedItem] = []
        #   for node in soup.select(entry["selector"]):
        #       title  = (node.select_one("a") or {}).get_text(strip=True)
        #       href   = (node.select_one("a") or {}).get("href")
        #       date_s = (node.select_one("time, .date") or {}).get_text(strip=True)
        #       items.append(IngestedItem(
        #           source="state_doi",
        #           source_id=f"{state}:{href}",
        #           title=f"[{state} DOI] {title}",
        #           url=href if href.startswith("http") else <urljoin>(url, href),
        #           author=agency,
        #           published_at=_parse(date_s),
        #           metadata={
        #               "topic_hint": "regulatory_rate",
        #               "state":      state,
        #               "agency":     agency,
        #           },
        #       ))
        #   return items
        return []
