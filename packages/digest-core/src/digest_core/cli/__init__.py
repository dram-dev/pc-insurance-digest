"""digest_core.cli — generic CLI building blocks.

A domain composes these into its own Click group + commands.
"""
from digest_core.cli.base import load_ingestor, run_ingest

__all__ = ["load_ingestor", "run_ingest"]
