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
