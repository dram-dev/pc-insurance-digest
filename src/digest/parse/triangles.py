"""Loss-development-triangle structurer (EKG Lead 6 — Reserve-Adequacy Radar).

Turns a `parse.pdf_tables.Table` that *is* a loss-development triangle into the
list-of-cell-dicts that `db.upsert_triangle_cells()` expects, closing the last
gap in the already-wired Option-5 reserving chain:

    investor_supp PDF
      → parse.pdf_tables.extract_tables() → Table
      → parse.triangles.parse_triangle() → [{insurer, lob, metric,
                                              accident_year, dev_period,
                                              cumulative_value, as_of}, …]   ← HERE
      → db.upsert_triangle_cells()
      → reserving.run_reserving()  (chain-ladder → reserving_signals)
      → db.reserving_severity_map()
      → signals._reserve_deterioration_boost()  → leaderboard

A loss-development triangle is a 2-D grid of cumulative paid (or incurred)
losses indexed by **accident year** (the year the loss occurred) on one axis
and **development period** (how many months/quarters of maturity have elapsed)
on the other. The classic layout is accident-year rows × development-period
columns, the upper-left "triangle" filled and the lower-right empty (recent
accident years have only early development observed):

        | dev 12 | dev 24 | dev 36 |
    2023|  100   |  150   |  165   |
    2024|  110   |  165   |        |
    2025|  120   |        |        |

This module auto-detects that orientation (and the transposed one), parses
accounting-formatted numbers ($, thousands commas, parenthesised negatives),
and skips subtotal / blank cells. Pure stdlib — no new dependencies — reusing
the `parse.pdf_tables` helpers (`Table`, `_norm`, and the
`fetch_pdf_bytes` / `extract_tables` / `find_tables` extraction path the
caller already uses).

Databricks-native upgrade
--------------------------
On a warehouse this hand-rolled orientation/number parsing is replaced by
`ai_parse_document()`, which returns structured tables (and handles scanned
PDFs via OCR) directly. That primitive is preview + heavier than Free Edition's
CPU-only tier supports, so this local-first path is the default; the warehouse
path is documented in docs/WAVE4_EKG_PLAN.md (Lead 6). The downstream
chain-ladder + boost wiring is identical regardless of which extractor produced
the cells.
"""
from __future__ import annotations

import logging
import re

from digest.parse.pdf_tables import Table, _norm

logger = logging.getLogger(__name__)

# An accident year is a 4-digit year in a plausible P&C reserving window.
_MIN_YEAR = 1980
_MAX_YEAR = 2100

# Cells that mark a subtotal / total / non-data row — never emit a triangle
# cell for these (matched against the row's *label*, case-insensitive).
_SUBTOTAL_RE = re.compile(
    r"\b(?:sub-?total|total|grand total|all years|aggregate|sum)\b",
    re.IGNORECASE,
)

# Tokens that mean "no observation here" inside a data cell.
_BLANK_TOKENS = {"", "-", "–", "—", "n/a", "na", "nm", "."}


def _parse_year(cell: str) -> int | None:
    """Extract a 4-digit accident year from a label cell, else None.

    Handles 'AY 2023', '2023', 'Accident Year 2024', 'CY2025'. Returns None for
    subtotal labels, blanks, and dev-period-looking values (e.g. '12').
    """
    if not cell or _SUBTOTAL_RE.search(cell):
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", cell)
    if not m:
        return None
    year = int(m.group(1))
    return year if _MIN_YEAR <= year <= _MAX_YEAR else None


def _parse_dev_period(cell: str) -> int | None:
    """Extract a development-period integer from a header cell, else None.

    Handles '12', '12 months', 'Month 12', 'Year 1', '24 mo.'. The absolute
    value doesn't matter to chain-ladder (it sorts ascending and develops
    positionally) — only the relative ordering does.
    """
    if not cell:
        return None
    # A bare 4-digit year header belongs to the *transposed* orientation; don't
    # treat it as a (huge) dev period here.
    if _parse_year(cell) is not None:
        return None
    m = re.search(r"\d+", cell)
    return int(m.group(0)) if m else None


def _parse_number(cell: str) -> float | None:
    """Parse an accounting-formatted number, else None.

    Strips $, thousands commas, trailing %, whitespace; treats (123) and -123
    as negative; maps blank/dash/n-a tokens to None (unobserved cell).
    """
    if cell is None:
        return None
    s = cell.strip()
    if s.lower() in _BLANK_TOKENS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    if s.startswith("-"):
        neg = True
        s = s[1:].strip()
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def _emit_cells(
    table: Table,
    *,
    insurer: str,
    lob: str,
    metric: str,
    as_of: str,
    transposed: bool,
) -> list[dict]:
    """Build cell dicts for one orientation.

    Standard  (transposed=False): rows = accident years, header = dev periods.
    Transposed(transposed=True ): rows = dev periods,    header = accident years.
    """
    header = [_norm(c) for c in table.header]
    cells: list[dict] = []

    if not transposed:
        # header[0] is the (possibly blank) row-label column; header[1:] = devs.
        dev_periods = [_parse_dev_period(h) for h in header[1:]]
        for row in table.rows:
            if not row:
                continue
            year = _parse_year(_norm(row[0]))
            if year is None:
                continue
            for col_ix, dev in enumerate(dev_periods, start=1):
                if dev is None or col_ix >= len(row):
                    continue
                val = _parse_number(_norm(row[col_ix]))
                if val is None:
                    continue
                cells.append(_cell(insurer, lob, metric, year, dev, val, as_of))
    else:
        # header[1:] = accident years; each row's first cell = dev period.
        years = [_parse_year(h) for h in header[1:]]
        for row in table.rows:
            if not row:
                continue
            dev = _parse_dev_period(_norm(row[0]))
            if dev is None:
                continue
            for col_ix, year in enumerate(years, start=1):
                if year is None or col_ix >= len(row):
                    continue
                val = _parse_number(_norm(row[col_ix]))
                if val is None:
                    continue
                cells.append(_cell(insurer, lob, metric, year, dev, val, as_of))

    return cells


def _cell(insurer: str, lob: str, metric: str, year: int, dev: int,
          val: float, as_of: str) -> dict:
    return {
        "insurer":          insurer,
        "lob":              lob,
        "metric":           metric,
        "accident_year":    year,
        "dev_period":       dev,
        "cumulative_value": val,
        "as_of":            as_of,
    }


def looks_like_triangle(table: Table) -> bool:
    """Heuristic gate: does this Table plausibly carry a development triangle?

    True when EITHER orientation yields ≥2 accident years and ≥2 development
    periods of real numeric cells — the minimum chain-ladder can develop.
    Cheap enough to call on every matched table before committing to parse.
    """
    return bool(parse_triangle(table, insurer="_", lob="_", metric="_", as_of="_",
                               _probe=True))


def parse_triangle(
    table: Table,
    *,
    insurer: str,
    lob: str,
    metric: str,
    as_of: str,
    _probe: bool = False,
) -> list[dict]:
    """Structure a triangle `Table` into loss_triangles cell dicts.

    Tries the standard (AY-row × dev-col) and transposed orientations and keeps
    whichever yields more cells. Returns [] when neither orientation produces a
    developable grid (≥2 accident years × ≥2 dev periods), so a non-triangle
    table that slipped through the header filter is silently ignored rather than
    polluting the triangle store.

    `metric` is 'paid' | 'incurred'; `as_of` is the disclosure quarter the
    triangle was reported as-of (ISO date string), used as the snapshot key for
    period-over-period deterioration in reserving.run_reserving().
    """
    standard   = _emit_cells(table, insurer=insurer, lob=lob, metric=metric,
                             as_of=as_of, transposed=False)
    transposed = _emit_cells(table, insurer=insurer, lob=lob, metric=metric,
                             as_of=as_of, transposed=True)
    cells = standard if len(standard) >= len(transposed) else transposed

    # Require a developable grid: ≥2 accident years and ≥2 dev periods.
    years = {c["accident_year"] for c in cells}
    devs = {c["dev_period"] for c in cells}
    if len(years) < 2 or len(devs) < 2:
        return []

    if _probe:
        return cells  # truthiness is all looks_like_triangle() needs
    logger.info(
        "triangles: %s/%s/%s as_of=%s — %d cells (%d AY × %d dev, %s)",
        insurer, lob, metric, as_of, len(cells), len(years), len(devs),
        "standard" if cells is standard else "transposed",
    )
    return cells
