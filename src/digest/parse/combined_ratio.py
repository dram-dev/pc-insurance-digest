"""Reported combined-ratio extraction from an investor-supplement / 10-K table.

The combined ratio is NOT XBRL-tagged, so it's read from the rendered financial-
highlights table (`parse.pdf_tables.Table`). A carrier's table carries combined
ratios for every segment + a consolidated total + sometimes a statutory variant,
so picking the right one from prose/position is fragile. Instead we VALIDATE
against an independent anchor — the XBRL-computed combined ratio (or a loss-ratio-
based estimate from digest.fundamentals) — and keep the candidate that reconciles.
Two independent sources agreeing (e.g. PGR's reported 87.4% vs the XBRL-derived
87.4%) is the soundest validation; if nothing reconciles, we return None rather
than guess. Pure / network-free.
"""
from __future__ import annotations

import logging
import re

from digest.parse.pdf_tables import Table

logger = logging.getLogger(__name__)

_CR_LABEL = re.compile(r"combined ratio", re.I)
_STATUTORY = re.compile(r"statutory", re.I)
_PCT = re.compile(r"^\(?-?\$?\s*(\d{2,3}(?:\.\d)?)\)?%?$")


def _row_value(cells: list[str]) -> float | None:
    """First plausible combined-ratio percentage (50-160) in a row's value cells."""
    for c in cells[1:]:
        m = _PCT.match(str(c).strip())
        if m:
            v = float(m.group(1))
            if 50.0 <= v <= 160.0:
                return v
    return None


def combined_ratio_candidates(tables: list[Table], *, gaap_only: bool = True) -> list[float]:
    """Every combined-ratio value (as a fraction) found in a table's rows. Statutory
    rows are skipped when gaap_only — the headline combined ratio is GAAP."""
    out: list[float] = []
    for t in tables:
        for row in t.rows:
            cells = [str(c).strip() for c in row]
            if not cells or not _CR_LABEL.search(cells[0]):
                continue
            if gaap_only and _STATUTORY.search(cells[0]):
                continue
            v = _row_value(cells)
            if v is not None:
                out.append(round(v / 100.0, 4))
    return out


def parse_combined_ratio(
    tables: list[Table], *, insurer: str, anchor: float | None, tolerance: float = 0.03,
) -> dict | None:
    """Pick the consolidated GAAP combined ratio (fraction) from `tables`, validated
    to within `tolerance` of `anchor`. Returns {combined_ratio, candidates} or None
    when nothing reconciles (or there's no anchor to validate against)."""
    candidates = combined_ratio_candidates(tables)
    if not candidates or anchor is None:
        return None
    best = min(candidates, key=lambda c: abs(c - anchor))
    if abs(best - anchor) > tolerance:
        logger.info("combined_ratio: %s — no candidate within %.0f pts of anchor %.3f (had %s)",
                    insurer, tolerance * 100, anchor, sorted(set(candidates)))
        return None
    return {"combined_ratio": best, "candidates": sorted(set(candidates))}
