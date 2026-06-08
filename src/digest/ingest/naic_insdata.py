"""NAIC InsData Schedule P loader — file-drop ingest for the big mutuals.

InsData is a download product (query → export), so the robust path is: the user
exports Schedule P to CSV from the InsData portal and drops the file(s) in
$NAIC_INSDATA_DIR; this loader reads them, maps columns via config/naic_insdata.yaml,
parses with digest.parse.schedule_p, and persists:
  • incurred/paid triangles → loss_triangles (feeds the chain-ladder reserving
    chain, identical to the SEC-XBRL route); and
  • earned-premium summary → statutory_facts.

This is the ONLY route to triangles for State Farm / USAA / Liberty Mutual /
Farmers / Nationwide — they file nothing with the SEC. No-ops cleanly when the
drop directory is empty or absent.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import yaml

from digest import db
from digest.config import settings
from digest.parse.schedule_p import parse_schedule_p

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "naic_insdata.yaml"
_SUFFIXES = {".csv", ".tsv", ".txt"}


def _read_records(path: Path) -> list[dict]:
    delimiter = "\t" if path.suffix.lower() in (".tsv", ".txt") else ","
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter))


def load_from_dir(directory: str | Path | None = None) -> dict:
    """Parse every Schedule P export in the drop dir → loss_triangles + statutory_facts."""
    drop = Path(directory or settings.naic_insdata_dir)
    if not drop.exists():
        logger.info("naic_insdata: drop dir %s does not exist — nothing to load. "
                    "Export Schedule P from InsData and place CSVs here.", drop)
        return {"files": 0, "triangle_cells": 0, "premium_facts": 0, "insurers": 0}

    cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    column_map = cfg.get("column_map", {})
    line_map = cfg.get("line_map", {})
    value_scale = float(cfg.get("value_scale", 1.0))

    files = sorted(p for p in drop.iterdir() if p.suffix.lower() in _SUFFIXES)
    all_cells: list[dict] = []
    all_facts: list[dict] = []
    for path in files:
        try:
            records = _read_records(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("naic_insdata: failed to read %s: %s", path.name, exc)
            continue
        cells, facts = parse_schedule_p(records, column_map, line_map=line_map,
                                        value_scale=value_scale)
        logger.info("naic_insdata: %s → %d cells, %d premium facts", path.name,
                    len(cells), len(facts))
        all_cells.extend(cells)
        all_facts.extend(facts)

    db.upsert_triangle_cells(all_cells)
    db.upsert_statutory_facts(all_facts)
    insurers = {c["insurer"] for c in all_cells} | {f["insurer"] for f in all_facts}
    return {"files": len(files), "triangle_cells": len(all_cells),
            "premium_facts": len(all_facts), "insurers": len(insurers),
            "insurer_list": sorted(insurers)}
