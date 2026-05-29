"""digest_core.ingest — framework ingestion base + source registry.

`IngestorBase` is the persist+log skeleton; `ItemStore` is the persistence
contract a domain binds via `store`. `IngestedItem` is re-exported for
convenience (it lives in `digest_core.types`).

The registry is the 'grow organically' seam: every concrete `IngestorBase`
subclass self-registers, and `discover(package)` imports a domain's ingest
modules so the catalog populates without a central dict. See `registry`.
"""
from digest_core.ingest.base import IngestedItem, IngestorBase, ItemStore
from digest_core.ingest.registry import (
    IngestorSpec,
    discover,
    get,
    import_failures,
    registered,
)

__all__ = [
    "IngestedItem",
    "IngestorBase",
    "ItemStore",
    "IngestorSpec",
    "discover",
    "get",
    "import_failures",
    "registered",
]
