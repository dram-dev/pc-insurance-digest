"""Full-text article extraction for excerpt-only feed items.

RSS/Atom and Substack feeds frequently ship only a teaser — the `summary`
field, a sentence or two — rather than the full article body. Summarizing a
teaser yields a summary of a summary, which is exactly the failure mode that
full-text readers (Mozilla Readability, trafilatura) were built to fix.

This module fetches the source URL and extracts the *main* article text, with
boilerplate (nav, ads, footers, comment threads) removed, so downstream triage
and summarization see the real content. Extraction uses trafilatura, the
de-facto main-content extractor.

Pure transport: the tuning knobs (enabled, thresholds, timeout) and the
User-Agent arrive as arguments, so a domain wraps this with its own settings.

Everything degrades gracefully: a missing dependency, a network error, a
paywall, or an empty extraction all leave the caller's original excerpt
untouched. Ingest never fails because of a full-text fetch.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def looks_like_excerpt(content: str | None, min_chars: int) -> bool:
    """A short body is almost certainly a feed teaser, not a full article."""
    return not content or len(content.strip()) < min_chars


def fetch_fulltext(
    url: str,
    *,
    user_agent: str,
    timeout_sec: int = 12,
    max_chars: int = 8000,
) -> str | None:
    """Fetch `url` and return its main article text, or None on any failure."""
    try:
        import trafilatura
    except ImportError:  # extraction is optional — never break ingest
        logger.debug("fulltext: trafilatura not installed; skipping %s", url)
        return None

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout_sec,
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
    return text.strip()[:max_chars]


def enrich(
    content: str | None,
    url: str | None,
    *,
    user_agent: str,
    enabled: bool = True,
    min_chars: int = 600,
    timeout_sec: int = 12,
    max_chars: int = 8000,
) -> str:
    """Return full article text when `content` is only a thin feed excerpt.

    Falls back to the original `content` (or "") whenever extraction is
    disabled, the URL is missing, the body already looks complete, or the
    fetch/parse yields nothing longer than what we started with.
    """
    base = content or ""
    if not enabled or not url or not looks_like_excerpt(content, min_chars):
        return base
    full = fetch_fulltext(
        url, user_agent=user_agent, timeout_sec=timeout_sec, max_chars=max_chars
    )
    if full and len(full) > len(base):
        logger.debug("fulltext: expanded %s (%d -> %d chars)", url, len(base), len(full))
        return full
    return base
