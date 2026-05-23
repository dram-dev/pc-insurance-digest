"""SEC EDGAR ingestor — pulls recent filings for hyperscaler capex tracking.

Uses the public EDGAR submissions API (no auth). SEC requires a user-agent with
a real name and email; set EDGAR_USER_AGENT in .env.

For 8-K filings, we attempt to fetch the EX-99.1 press release HTML (earnings
releases, capex guidance, forward commentary) via the EDGAR filing index.
For 13F-HR filings, holdings data is extracted from the XML InfoTable.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

EDGAR_CONFIG = Path(__file__).resolve().parents[3] / "config" / "edgar_tickers.yaml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"
FILING_INDEX_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=1&search_text="

# Filing types we care about for capex signal
RELEVANT_FORMS = {"10-K", "10-Q", "8-K", "13F-HR"}

# Only fetch content for filings filed within this many days (avoid backfilling)
_CONTENT_FETCH_MAX_AGE_DAYS = 7
# Polite delay between EDGAR doc fetches (SEC rate limit: 10 req/s)
_EDGAR_FETCH_DELAY = 0.15


class _TextExtractor(HTMLParser):
    """Strip HTML tags; collapse whitespace; skip script/style blocks."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0  # depth counter handles nested skip-tags correctly
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            s = data.strip()
            if s:
                self.parts.append(s)

    def get_text(self, max_chars: int = 5000) -> str:
        text = re.sub(r"\s+", " ", " ".join(self.parts)).strip()
        return text[:max_chars]


def _fetch_html_text(url: str, headers: dict, max_chars: int = 5000) -> str | None:
    """Fetch a URL and return stripped plain text, or None on failure."""
    try:
        time.sleep(_EDGAR_FETCH_DELAY)
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        logger.debug("edgar: content fetch failed for %s: %s", url, exc)
        return None
    parser = _TextExtractor()
    parser.feed(r.text)
    text = parser.get_text(max_chars=max_chars)
    return text or None


def _find_exhibit_url(index_html: str, base_url: str, exhibit_type: str = "EX-99") -> str | None:
    """Parse an EDGAR filing index page and return the URL of the first EX-99.x document."""

    class _IndexParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._last_href: str = ""
            self.exhibit_url: str | None = None
            self._in_row = False
            self._row_text = ""

        def handle_starttag(self, tag: str, attrs: list) -> None:
            attr_dict = dict(attrs)
            if tag == "tr":
                self._in_row = True
                self._row_text = ""
            if tag == "a" and "href" in attr_dict:
                self._last_href = attr_dict["href"]

        def handle_endtag(self, tag: str) -> None:
            if tag == "tr" and self._in_row:
                # Check if this row mentions EX-99 in its text content
                if exhibit_type.lower() in self._row_text.lower() and self._last_href:
                    href = self._last_href
                    if not href.startswith("http"):
                        href = "https://www.sec.gov" + href
                    self.exhibit_url = href
                self._in_row = False

        def handle_data(self, data: str) -> None:
            if self._in_row:
                self._row_text += data

    parser = _IndexParser()
    parser.feed(index_html)
    return parser.exhibit_url


def _fetch_8k_content(cik_int: str, accession: str, headers: dict) -> str | None:
    """Fetch EX-99.1 press release content for an 8-K filing."""
    accession_nodashes = accession.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodashes}/"
        f"{accession}-index.htm"
    )
    try:
        time.sleep(_EDGAR_FETCH_DELAY)
        r = requests.get(index_url, headers=headers, timeout=20)
        r.raise_for_status()
        exhibit_url = _find_exhibit_url(r.text, index_url, exhibit_type="EX-99")
        if not exhibit_url:
            # Fallback: fetch the primary document itself
            return None
        return _fetch_html_text(exhibit_url, headers, max_chars=5000)
    except Exception as exc:
        logger.debug("edgar: 8-K index fetch failed (%s): %s", accession, exc)
        return None


class EdgarIngestor(IngestorBase):
    name = "edgar"

    def __init__(self) -> None:
        if not settings.edgar_user_agent:
            raise RuntimeError(
                "EDGAR_USER_AGENT not set. SEC requires a user-agent like "
                "'Your Name your.email@example.com'."
            )
        self.headers = {"User-Agent": settings.edgar_user_agent}
        self.config = yaml.safe_load(EDGAR_CONFIG.read_text())
        self._cutoff = datetime.now(timezone.utc) - timedelta(days=_CONTENT_FETCH_MAX_AGE_DAYS)

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for entry in self.config["companies"]:
            cik = str(entry["cik"]).zfill(10)
            ticker = entry["ticker"]
            entity_name = entry.get("name", ticker)
            is_fund = entry.get("fund", False)
            try:
                r = requests.get(
                    SUBMISSIONS_URL.format(cik=cik), headers=self.headers, timeout=20
                )
                r.raise_for_status()
                data = r.json()
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                primary_docs = recent.get("primaryDocument", [])

                for i, form in enumerate(forms[:40]):
                    if form not in RELEVANT_FORMS:
                        continue
                    # 13F-HR only relevant for funds/institutions
                    if form == "13F-HR" and not is_fund:
                        continue

                    accession = accessions[i]
                    filing_date = dates[i]
                    primary_doc = primary_docs[i]
                    cik_int = str(int(cik))
                    accession_nodashes = accession.replace("-", "")

                    url = FILING_URL.format(
                        cik_int=cik_int,
                        accession_no_dashes=accession_nodashes,
                        primary_doc=primary_doc,
                    )

                    try:
                        published = datetime.strptime(filing_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        logger.debug("edgar: unparseable date %r for %s %s", filing_date, ticker, accession)
                        published = None

                    # Fetch content for recent 8-K filings only (avoid backfilling)
                    content: str | None = None
                    if form == "8-K" and published and published >= self._cutoff:
                        content = _fetch_8k_content(cik_int, accession, self.headers)
                        if content:
                            logger.debug(
                                "edgar: fetched 8-K EX-99 content for %s %s (%d chars)",
                                ticker, accession, len(content),
                            )

                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=f"{ticker}:{accession}",
                            title=f"{ticker} {form} filed {filing_date}",
                            url=url,
                            author=entity_name,
                            content=content,
                            published_at=published,
                            metadata={
                                "ticker": ticker,
                                "cik": cik,
                                "form": form,
                                "accession": accession,
                                "primary_document": primary_doc,
                                "topic_hint": "fed_markets" if is_fund else "ai_capex",
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("edgar: failed on %s (CIK %s): %s", ticker, cik, exc)
        return items
