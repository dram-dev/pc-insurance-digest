"""Ingest component-level insurer XBRL facts (concept registry) into the DB.

One instance fetch per insurer → digest.parse.xbrl_facts.extract_facts pulls
every registered dataset's component facts → insurer_xbrl_facts. The incurred/
paid triangle facts are also reshaped into loss_triangles so the existing
chain-ladder reserving chain keeps feeding. Universe = config/xbrl_pc_insurers.yaml
(the top-10 SEC-filing US P&C underwriters).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from digest import db
from digest.edgar_triangle_extract import fetch_instance_xml
from digest.parse.xbrl_facts import extract_facts, triangle_cells_from_facts

logger = logging.getLogger(__name__)

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "xbrl_pc_insurers.yaml"


def insurer_universe() -> list[tuple[str, str]]:
    """[(ticker, zero-padded CIK)] for the configured top-10 P&C insurers."""
    cfg = yaml.safe_load(_CONFIG.read_text())
    return [(c["ticker"], str(c["cik"]).zfill(10)) for c in cfg["insurers"]]


def ingest_one(ticker: str, cik: str) -> dict:
    """Fetch one insurer's latest-10-K instance, extract + persist its facts."""
    instance, filed = fetch_instance_xml(cik)
    facts = extract_facts(instance, insurer=ticker)
    db.upsert_xbrl_facts(facts)
    cells = triangle_cells_from_facts(facts)
    db.upsert_triangle_cells(cells)
    return {
        "ticker": ticker, "filed": filed,
        "facts": len(facts), "triangle_cells": len(cells),
        "datasets": sorted({f["dataset"] for f in facts}),
    }


def run_ingest(tickers: list[str] | None = None) -> list[dict]:
    """Ingest the configured universe (or a ticker subset). Per-insurer best-effort."""
    universe = insurer_universe()
    if tickers:
        want = {t.upper() for t in tickers}
        universe = [(t, c) for t, c in universe if t in want]
    results: list[dict] = []
    for ticker, cik in universe:
        try:
            results.append(ingest_one(ticker, cik))
        except Exception as exc:  # noqa: BLE001 — one filer shouldn't abort the run
            logger.warning("xbrl ingest failed for %s: %s", ticker, exc)
            results.append({"ticker": ticker, "error": str(exc)})
    return results
