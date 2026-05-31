"""Shared date parsing for ingestors.

Each scraper used to maintain its own `_parse_date` with an overlapping
list of strptime formats. The union is small enough to live in one place;
callers needing a narrower set pass `formats=` explicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

_DEFAULT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y",       # January 15, 2026
    "%b %d, %Y",       # Jan 15, 2026
    "%b. %d, %Y",      # Jan. 15, 2026
    "%d %B %Y",        # 15 January 2026
)


def parse_date(text: str | None, formats: Iterable[str] | None = None) -> datetime | None:
    """Try each strptime format; return the first match as a UTC-aware datetime."""
    if not text:
        return None
    clean = text.strip()
    # ISO-8601 fast path (e.g. <time datetime="2026-05-28T13:42:01-04:00">) —
    # strptime formats don't cover tz offsets. Normalize to UTC.
    if not formats:
        try:
            dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in formats or _DEFAULT_FORMATS:
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
