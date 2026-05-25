"""Pipeline sinks — secondary write destinations alongside SQLite.

Currently exposes a single Databricks medallion sink. Future sinks (BigQuery,
S3 parquet, etc.) follow the same pattern: best-effort writes, no-op when
disabled, never bricks the local pipeline.

Import the module-level `sink` singleton and call its methods next to existing
SQLite writes; when the feature flag is off, every call is a fast no-op.
"""
from __future__ import annotations

from digest.config import settings
from digest.sinks.databricks import DatabricksSink

sink: DatabricksSink = DatabricksSink(settings)

__all__ = ["DatabricksSink", "sink"]
