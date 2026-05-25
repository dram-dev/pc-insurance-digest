"""NAIC Schedule P loss-triangle ingestor — scaffold pending data access.

Annual cadence. Schedule P discloses calendar-year-by-accident-year loss
development by line of business — the canonical adverse-development
signal for P&C reserving and social_inflation watch.

**Data-access status (2026-05-25):** Bulk Schedule P data is not freely
available. Real sources require either:
  - NAIC InsData subscription (paid)
  - State DOI annual-statement filings (per-state, inconsistent formats)
  - AM Best statutory database (paid)
  - Carrier 10-K statutory exhibits (uncommon for public carriers)

This module is a scaffold. The ingestor `fetch()` no-ops while
`config/naic_schedp_sources.yaml` `sources:` is empty. Plumbing
(auto-keep hook, cli registration, summarize stub) is wired so once
a data source lands we just point the config at it.

When wiring real fetch: emit one IngestedItem per (insurer, line-of-business)
triangle with metadata.line_of_business + metadata.adverse_dev_pct so
the leaderboard scoring can flag adverse development.

Wave 3 Phase 3 — Liability Intelligence cluster (scaffold).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "naic_schedp_sources.yaml"


class NAICSchedulePIngestor(IngestorBase):
    name = "naic_schedp"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"naic_schedp config missing: {_CONFIG_PATH}")
        cfg = yaml.safe_load(_CONFIG_PATH.read_text())
        self.defaults: dict[str, Any] = cfg.get("defaults", {}) or {}
        self.sources: list[dict[str, Any]] = cfg.get("sources", []) or []

    def fetch(self) -> list[IngestedItem]:
        enabled = [s for s in self.sources if s.get("enabled", False)]
        if not enabled:
            logger.info(
                "naic_schedp: no sources enabled — NAIC Schedule P data is paid "
                "(NAIC InsData / AM Best) or state-by-state. Add a `sources:` "
                "entry in config/naic_schedp_sources.yaml once access is secured."
            )
            return []
        # Real per-source dispatch goes here when data access is sorted.
        # Each source type (insdata_api, state_doi_annual, etc.) gets a
        # _fetch_<kind>(entry) helper.
        return []
