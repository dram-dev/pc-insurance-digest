"""Pipeline sinks — secondary write destinations alongside SQLite.

The Databricks sink implementation lives in `digest_core.sinks.databricks`;
this module constructs the singleton from PC Digest's settings.
"""
from __future__ import annotations

from digest.config import settings
from digest_core.sinks.databricks import DatabricksSink, item_hash

sink: DatabricksSink = DatabricksSink(
    enabled=settings.databricks_enabled,
    host=settings.databricks_host,
    http_path=settings.databricks_http_path,
    token=settings.databricks_token,
    catalog=settings.databricks_catalog,
)

__all__ = ["DatabricksSink", "item_hash", "sink"]
