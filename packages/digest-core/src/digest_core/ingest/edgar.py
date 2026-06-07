"""EDGAR filing-content helpers — HTML→text + EX-99 exhibit fetching.

The reusable, domain-agnostic parts of EDGAR ingestion: stripping filing HTML
to plain text, locating the EX-99.x exhibit on a filing index page, and pulling
an 8-K's press-release body. A domain's EDGAR ingestor owns the ticker/CIK
universe + topic mapping and calls these for body content.
"""
from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser

import requests

logger = logging.getLogger(__name__)

# Polite delay between EDGAR doc fetches (SEC rate limit: 10 req/s).
EDGAR_FETCH_DELAY = 0.15


class TextExtractor(HTMLParser):
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


def fetch_html_text(
    url: str,
    headers: dict,
    max_chars: int = 5000,
    delay_sec: float = EDGAR_FETCH_DELAY,
) -> str | None:
    """Fetch a URL and return stripped plain text, or None on failure."""
    try:
        time.sleep(delay_sec)
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.debug("edgar: content fetch failed for %s: %s", url, exc)
        return None
    parser = TextExtractor()
    parser.feed(r.text)
    text = parser.get_text(max_chars=max_chars)
    return text or None


def find_exhibit_url(index_html: str, base_url: str, exhibit_type: str = "EX-99") -> str | None:
    """Parse an EDGAR filing index page; return the first EX-99.x document URL."""

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


def fetch_8k_content(
    cik_int: str,
    accession: str,
    headers: dict,
    delay_sec: float = EDGAR_FETCH_DELAY,
    max_chars: int = 5000,
) -> str | None:
    """Fetch EX-99.1 press-release content for an 8-K filing (None if absent).

    `max_chars` caps the extracted text; the default of 5000 preserves prior
    behavior, but carriers (e.g. Progressive) put their combined-ratio and
    reserve-development tables deeper in the release, so callers wanting those
    figures should pass a larger cap.
    """
    accession_nodashes = accession.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodashes}/"
        f"{accession}-index.htm"
    )
    try:
        time.sleep(delay_sec)
        r = requests.get(index_url, headers=headers, timeout=20)
        r.raise_for_status()
        exhibit_url = find_exhibit_url(r.text, index_url, exhibit_type="EX-99")
        if not exhibit_url:
            # Fallback: caller fetches the primary document itself.
            return None
        return fetch_html_text(exhibit_url, headers, max_chars=max_chars, delay_sec=delay_sec)
    except Exception as exc:  # noqa: BLE001
        logger.debug("edgar: 8-K index fetch failed (%s): %s", accession, exc)
        return None
