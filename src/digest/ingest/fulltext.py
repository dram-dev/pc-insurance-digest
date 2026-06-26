"""Full-text article extraction for excerpt-only feed items.

RSS/Atom and Substack feeds frequently ship only a teaser — the `summary`
field, a sentence or two — rather than the full article body. Summarizing a
teaser yields a summary of a summary, which is exactly the failure mode that
full-text readers (Mozilla Readability, trafilatura) were built to fix.

This module fetches the source URL and extracts the *main* article text, with
boilerplate (nav, ads, footers, comment threads) removed, so downstream triage
and MLX summarization see the real content. Extraction uses trafilatura, the
de-facto main-content extractor.

Everything degrades gracefully: a missing dependency, a network error, a
paywall, or an empty extraction all leave the caller's original excerpt
untouched. Ingest never fails because of a full-text fetch.
"""
from __future__ import annotations

import logging

import requests

from digest.config import settings

logger = logging.getLogger(__name__)

# Polite, identifiable UA — some publishers 403 a bare python-requests UA.
_UA = "pc-insurance-digest/0.1 (+https://github.com/dram-dev/pc-insurance-digest)"


def _looks_like_excerpt(content: str | None) -> bool:
    """A short body is almost certainly a feed teaser, not a full article."""
    return not content or len(content.strip()) < settings.fulltext_min_chars


def fetch_fulltext(url: str) -> str | None:
    """Fetch `url` and return its main article text, or None on any failure."""
    try:
        import trafilatura
    except ImportError:  # extraction is optional — never break ingest
        logger.debug("fulltext: trafilatura not installed; skipping %s", url)
        return None

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=settings.fulltext_timeout_sec,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.debug("fulltext: fetch failed for %s: %s", url, exc)
        return None

    try:
        text = trafilatura.extract(
            resp.text,
            url=url,
            include_comments=False,
            include_tables=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("fulltext: extract failed for %s: %s", url, exc)
        return None

    if not text:
        return None
    return text.strip()[: settings.fulltext_max_chars]


def enrich(content: str | None, url: str | None) -> str:
    """Return full article text when `content` is only a thin feed excerpt.

    Falls back to the original `content` (or "") whenever extraction is
    disabled, the URL is missing, the body already looks complete, or the
    fetch/parse yields nothing longer than what we started with.
    """
    base = content or ""
    if not settings.fulltext_enabled or not url or not _looks_like_excerpt(content):
        return base
    full = fetch_fulltext(url)
    if full and len(full) > len(base):
        logger.debug("fulltext: expanded %s (%d -> %d chars)", url, len(base), len(full))
        return full
    return base
