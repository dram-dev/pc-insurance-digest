"""DatabricksSink — re-export shim.

The implementation moved to `digest_core.sinks.databricks` in the
digest-core extraction branch (2026-05-25). This shim preserves the
`from digest.sinks.databricks import DatabricksSink, item_hash` import
path for any existing callers. The module-level `sink` singleton lives
in `digest.sinks.__init__` since constructing it needs PC Digest's
settings module.
"""
from __future__ import annotations

from digest_core.sinks.databricks import DatabricksSink, item_hash

__all__ = ["DatabricksSink", "item_hash"]
