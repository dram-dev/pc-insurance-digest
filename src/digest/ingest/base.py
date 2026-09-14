"""PC-domain ingestor base — binds digest_core's IngestorBase to PC persistence.

The framework base (fetch → persist → log skeleton) lives in
`digest_core.ingest.base` and stays domain-agnostic by taking persistence
through an `ItemStore`. PC binds that store to `digest.db` here — its
module-level `upsert_items` / `log_run` satisfy the `ItemStore` contract — so
the ingestors keep importing `IngestorBase` / `IngestedItem` from this module
unchanged. `IngestedItem` is re-exported from `digest_core.types`.
"""
from __future__ import annotations

from digest_core.ingest.base import IngestorBase as _CoreIngestorBase
from digest_core.types import IngestedItem

from digest import db

__all__ = ["IngestedItem", "IngestorBase"]


class IngestorBase(_CoreIngestorBase):
    """PC ingestor base. Persistence is bound to `digest.db`."""

    store = db

    def enrich_items(self, items: list[IngestedItem]) -> list[IngestedItem]:
        """Expand excerpt-only feed entries into full article text.

        Bound here rather than in each ingestor so any feed-backed source opts
        in with one class attribute. Items already stored are skipped: the fetch
        is the expensive part and `upsert_items` would discard the result.
        """
        if not self.enrich_fulltext:
            return items
        from digest.ingest.fulltext import enrich

        seen = db.existing_source_ids(self.name)
        for item in items:
            url = self.enrich_url(item)
            if url and item.source_id not in seen:
                item.content = enrich(item.content, url)
        return items
