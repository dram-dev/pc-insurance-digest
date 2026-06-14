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


def ingest_one(ticker: str, cik: str, n: int = 0) -> dict:
    """Fetch one insurer's n-th most recent 10-K instance (n=0 latest annual
    diagonal, n=1 prior year, …), extract + persist its facts and triangle cells.

    The loss-triangle `as_of` is the filing's fiscal year-end (derived from the XBRL
    fact contexts), so two diagonals land as two distinct snapshots rather than
    clobbering one another on the (insurer, lob, metric, AY, dev, as_of) PK."""
    instance, filed = fetch_instance_xml(cik, n=n)
    facts = extract_facts(instance, insurer=ticker)
    db.upsert_xbrl_facts(facts)
    cells = triangle_cells_from_facts(facts)
    db.upsert_triangle_cells(cells)
    as_of = max((f["as_of"] for f in facts if f.get("as_of")), default=None)
    return {
        "ticker": ticker, "n": n, "filed": filed, "as_of": as_of,
        "facts": len(facts), "triangle_cells": len(cells),
        "datasets": sorted({f["dataset"] for f in facts}),
    }


def run_ingest(tickers: list[str] | None = None, *, diagonals: int = 1) -> list[dict]:
    """Ingest the configured universe (or a ticker subset), `diagonals` annual
    snapshots deep per insurer.

    diagonals=1 (default) = latest 10-K only — the original behaviour. diagonals≥2
    also pulls each insurer's prior-year 10-K(s) so reserve_deterioration_boost has
    ≥2 as_of snapshots to compare (it stays neutral at 1.0 on a single diagonal).
    Best-effort per (insurer, diagonal): a missing prior 10-K or one bad filer logs
    and is skipped rather than aborting the run."""
    universe = insurer_universe()
    if tickers:
        want = {t.upper() for t in tickers}
        universe = [(t, c) for t, c in universe if t in want]
    results: list[dict] = []
    for ticker, cik in universe:
        for n in range(max(1, diagonals)):
            try:
                results.append(ingest_one(ticker, cik, n=n))
            except Exception as exc:  # noqa: BLE001 — one filing shouldn't abort the run
                logger.warning("xbrl ingest failed for %s (diagonal n=%d): %s", ticker, n, exc)
                results.append({"ticker": ticker, "n": n, "error": str(exc)})
    return results
