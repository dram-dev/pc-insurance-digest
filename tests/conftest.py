"""Shared pytest fixtures.

The suite runs hermetically: every test gets a throwaway SQLite file and the
Databricks `sink` is forced off, so nothing touches external services even
though the developer `.env` may have `DATABRICKS_ENABLED=true`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from digest import db
from digest.ingest.base import IngestedItem


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the digest DB at a fresh temp file, init the schema, disable the sink."""
    path = tmp_path / "test_state.db"
    monkeypatch.setattr(db.settings, "db_path", path)
    # `_enabled` is the documented kill switch every sink.write_* method guards on.
    monkeypatch.setattr(db.sink, "_enabled", False)
    db.init_db(path)
    return path


@pytest.fixture
def make_item():
    """Factory for IngestedItem rows with sensible defaults."""
    def _make(
        source: str = "rss",
        source_id: str = "a1",
        title: str = "A title",
        *,
        url: str | None = "https://example.test/x",
        author: str | None = None,
        content: str | None = "body",
        published_at: datetime | None = None,
        metadata: dict | None = None,
    ) -> IngestedItem:
        return IngestedItem(
            source=source,
            source_id=source_id,
            title=title,
            url=url,
            author=author,
            content=content,
            published_at=published_at,
            metadata=metadata or {},
        )

    return _make
