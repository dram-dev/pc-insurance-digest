"""Secondary sinks — write destinations layered alongside SQLite.

Domain projects construct sink instances from their own settings and call
the sink methods next to existing SQLite writes. When the feature flag is
off every call is a fast no-op.
"""
from digest_core.sinks.databricks import DatabricksSink, item_hash

__all__ = ["DatabricksSink", "item_hash"]
