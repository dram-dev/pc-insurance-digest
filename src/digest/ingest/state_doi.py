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

# Several DOIs are multi-line regulators that also run the ACA marketplace
# (IDOI, NJ DOBI, MI DIFS) — their press feeds are health-heavy. auto_keep_state_doi
# keeps every state_doi item without re-triage, so we drop health-insurance content
# at the source. Word-boundary matched; opt out per state with `drop_health: false`.
_HEALTH_DENYLIST: tuple[str, ...] = (
    "get covered",                  # Get Covered Illinois / New Jersey (ACA brands)
    "open enrollment",
    "affordable care act",
    "obamacare",
    "health insurance marketplace",
    "health benefit exchange",
    "health coverage",
    "health insurance",
    "health plan",
    "medicaid",
    "medicare",
    "mental health",
    "behavioral health",
    "prescription drug",
    "premium tax credit",           # ACA subsidy framing
    "covered california",
    "nevada health link",
)

# "Month DD, YYYY" embedded in a node's text (NV/NJ encode the date there, not
# in a dedicated element). The leading-prefix variant also strips it off a title.
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_DATE_IN_TEXT = re.compile(rf"({_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}", re.I)
_DATE_PREFIX = re.compile(rf"^\s*(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\s*[-–—:]\s*", re.I)


def _kw_hit(blob: str, keywords) -> bool:
    """True if any keyword appears in `blob` on word boundaries, tolerating a
    trailing plural "s" (so "policyholder" matches "policyholders" and "homeowner"
    matches "homeowners insurance" — but neither matches "homeownership").
    Case-insensitive. Keep keywords in singular base form."""
    return any(re.search(rf"\b{re.escape(k)}s?\b", blob, re.IGNORECASE) for k in keywords)


class StateDOIIngestor(IngestorBase):
    name = "state_doi"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"state_doi config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())

    def _pc_keywords(self, entry: dict) -> list[str]:
        """The P&C allowlist for a state that opts in (`pc_allowlist: true`):
        the shared `defaults.pc_keywords` plus any `pc_keywords_extra`. Empty
        list = no allowlist (keep everything that survives the health denylist)."""
        if not entry.get("pc_allowlist"):
            return []
        base = self.config.get("defaults", {}).get("pc_keywords", [])
        return [k.lower() for k in [*base, *entry.get("pc_keywords_extra", [])]]

    def _passes_filters(self, blob: str, entry: dict) -> bool:
        """Drop health-insurance content (default, per the user's no-health rule)
        and — for opted-in multi-domain regulators — anything without a P&C signal."""
        blob = blob.lower()
        if entry.get("drop_health", True) and _kw_hit(blob, _HEALTH_DENYLIST):
            return False
        pc = self._pc_keywords(entry)
        if pc and not _kw_hit(blob, pc):
            return False
        return True

    def _build_item(
        self, state: str, agency: str, title: str, href: str, pub
    ) -> IngestedItem:
        return IngestedItem(
            source=self.name,
            source_id=f"{state}:{urlparse(href).path}",
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
        # `json_feed` states (IL IDOI) render their press list from a JS widget
        # backed by a clean Sling model JSON — hit that endpoint directly rather
        # than the SPA (same "clean data path behind a JS UI" play as CA's xlsx /
        # FL's JSON API). No HTML parsing, no headless browser.
        if entry.get("json_feed"):
            return self._scrape_json_feed(state, agency, url, entry)

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
        # (LA LDI lists ~97 releases back to 2024). Cap on KEPT items, not raw
        # nodes, so the health/P&C filter can't starve the result below the cap.
        max_items = int(entry["max_items"]) if entry.get("max_items") else None

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
            # `strip_date_prefix` drops a leading "Month DD, YYYY - " from titles
            # that bake the date into the headline (NV "May 13, 2026 - Annual …").
            if entry.get("strip_date_prefix"):
                title = _DATE_PREFIX.sub("", title)
            if not title:
                continue

            # Href: prefer first anchor
            a = node.select_one("a[href]")
            href = a.get("href", "") if a else ""
            if href and not href.startswith("http"):
                href = urljoin(url, href)
            if not href or href in seen_urls:
                continue

            # No-health rule + optional P&C allowlist, matched against the row text.
            if not self._passes_filters(node.get_text(" ", strip=True), entry):
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
            if pub is None and entry.get("date_from_text"):
                # NV/NJ carry the date only as "Month DD, YYYY" in the row text.
                m = _DATE_IN_TEXT.search(node.get_text(" ", strip=True))
                if m:
                    pub = parse_date(re.sub(r"\s+", " ", m.group(0)))

            items.append(self._build_item(state, agency, title, href, pub))
            if max_items and len(items) >= max_items:
                break

        logger.info("state_doi: %s — %d items scraped", state, len(items))
        return items

    def _scrape_json_feed(
        self,
        state: str,
        agency: str,
        url: str,
        entry: dict,
    ) -> list[IngestedItem]:
        """Parse a JSON news feed — IL IDOI (AEM/Sling model) and MI DIFS
        (Sitecore SXA search/results) both expose one behind their JS widgets.

        The list lives at `json_list_key` (IL ``newsFeedItemList`` / MI ``Results``).
        Per item, title+url come from flat fields (`json_title_field`/`json_url_field`)
        unless `json_html_field` is set — then each record carries a rendered HTML
        fragment (MI) and the title/href are parsed out of it via
        `json_html_title_selector`. Dates come from `date`+`year` fields (IL) or,
        failing that, a /YYYY/MM/DD/ path in the URL (MI). Because auto_keep keeps
        every state_doi item un-triaged, the shared health/P&C filter applies here
        too — essential since these regulators are health/banking-heavy.
        """
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        records = r.json().get(entry.get("json_list_key", "newsFeedItemList"), [])

        title_field = entry.get("json_title_field", "title")
        url_field   = entry.get("json_url_field", "url")
        desc_field  = entry.get("json_desc_field", "description")
        html_field  = entry.get("json_html_field")
        html_sel    = entry.get("json_html_title_selector")
        date_field  = entry.get("json_date_field", "date")
        year_field  = entry.get("json_year_field", "year")
        max_items   = entry.get("max_items")

        items: list[IngestedItem] = []
        seen: set[str] = set()
        for rec in records:
            desc = rec.get(desc_field) or ""
            if html_field and rec.get(html_field):
                frag = BeautifulSoup(rec[html_field], "html.parser")
                node = frag.select_one(html_sel) if html_sel else frag.find("a", href=True)
                title = node.get_text(" ", strip=True) if node else ""
                href = (rec.get(url_field) or (node.get("href", "") if node else "")).strip()
            else:
                title = (rec.get(title_field) or "").strip()
                href = (rec.get(url_field) or "").strip()
            if not title or not href:
                continue
            if not href.startswith("http"):
                href = urljoin(url, href)
            if href in seen:
                continue
            if not self._passes_filters(f"{title} {desc}", entry):
                continue
            seen.add(href)

            # Date: "Wednesday, July 30" + a separate "year" → strptime-parseable
            # "July 30, 2025" (IL); else a /YYYY/MM/DD/ in the URL path (MI).
            pub = None
            if rec.get(date_field):
                day = re.sub(r"^[A-Za-z]+,\s*", "", str(rec[date_field]).strip())
                pub = parse_date(f"{day}, {rec.get(year_field, '')}".strip(", "))
            if pub is None:
                m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)", href)
                if m:
                    y, mo, d = (int(g) for g in m.groups())
                    pub = parse_date(f"{y:04d}-{mo:02d}-{d:02d}")

            items.append(self._build_item(state, agency, title, href, pub))
            if max_items and len(items) >= int(max_items):
                break

        logger.info("state_doi: %s — %d items from JSON feed", state, len(items))
        return items
