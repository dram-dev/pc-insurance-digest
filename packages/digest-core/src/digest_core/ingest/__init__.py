"""digest_core.ingest — framework ingestion base.

`IngestorBase` is the persist+log skeleton; `ItemStore` is the persistence
contract a domain binds via `store`. `IngestedItem` is re-exported for
convenience (it lives in `digest_core.types`).
"""
from digest_core.ingest.base import IngestedItem, IngestorBase, ItemStore

__all__ = ["IngestedItem", "IngestorBase", "ItemStore"]
