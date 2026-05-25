"""Shared dataclass shapes for digest-style projects.

`IngestedItem` is the normalized in-memory shape every ingestor produces
before triage / summarization. Domain ingestors may attach domain-specific
keys into `metadata`, but the top-level fields are framework-owned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class IngestedItem:
    """Normalized item from any source, before triage/summarization."""

    source: str                              # e.g. 'rss' | 'edgar' | 'fred' | 'reddit' | 'hn'
    source_id: str                           # unique within source
    title: str
    url: str | None = None
    author: str | None = None
    content: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
