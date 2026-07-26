"""Full-text article extraction — PC domain shell.

Mechanics live in `digest_core.ingest.fulltext` (pure transport); this shell
binds the `FULLTEXT_*` settings and the polite User-Agent. Kept as a module so
`from digest.ingest.fulltext import enrich` stays the import everywhere.
"""
from __future__ import annotations

from digest_core.ingest import fulltext as _core

from digest.config import settings

__all__ = ["enrich", "fetch_fulltext"]

# Polite, identifiable UA — some publishers 403 a bare python-requests UA.
_UA = "pc-insurance-digest/0.1 (+https://github.com/dram-dev/pc-insurance-digest)"


def fetch_fulltext(url: str) -> str | None:
    """Fetch `url` and return its main article text, or None on any failure."""
    return _core.fetch_fulltext(
        url,
        user_agent=_UA,
        timeout_sec=settings.fulltext_timeout_sec,
        max_chars=settings.fulltext_max_chars,
    )


def enrich(content: str | None, url: str | None) -> str:
    """Return full article text when `content` is only a thin feed excerpt."""
    return _core.enrich(
        content,
        url,
        user_agent=_UA,
        enabled=settings.fulltext_enabled,
        min_chars=settings.fulltext_min_chars,
        timeout_sec=settings.fulltext_timeout_sec,
        max_chars=settings.fulltext_max_chars,
    )
