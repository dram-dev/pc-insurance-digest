"""PDF table extraction helpers.

Wraps pdfplumber with the patterns ingestors actually need: GET a URL,
open it as a PDF, walk pages, extract tables, filter by header pattern.

Consumers: `src/digest/ingest/investor_supp.py`,
`src/digest/ingest/naic_schedp.py`. Add new ingestors that need PDF
tables here by importing `fetch_pdf_bytes`, `extract_tables`, and
`find_tables`.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Iterable

import pdfplumber
import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Table:
    """One table extracted from a PDF page."""
    page: int                  # 1-indexed PDF page
    header: list[str]          # first row, whitespace-normalized
    rows: list[list[str]]      # remaining rows
    caption: str = ""          # text line directly above the grid (title/LOB/basis)

    @property
    def search_text(self) -> str:
        """Caption + header — what header-pattern matching and LOB/metric
        attribution should scan. In real disclosures the line-of-business and
        the paid/incurred basis live in the table's *caption*, not its column
        header row, so detection that ignores the caption silently fails."""
        return f"{self.caption} {' '.join(self.header)}".strip()

    def header_matches(self, patterns: Iterable[str]) -> bool:
        """True if the caption-or-header text matches any regex in `patterns`.

        Caption-aware: `caption` defaults to '' so Tables built without one
        (tests, other callers) match on the header exactly as before."""
        joined = self.search_text.lower()
        return any(re.search(p, joined, re.IGNORECASE) for p in patterns)

    def to_text(self, max_rows: int = 30) -> str:
        """Pipe-delimited render for storage in items.content or summary."""
        lines = [" | ".join(self.header)]
        for row in self.rows[:max_rows]:
            lines.append(" | ".join(row))
        if len(self.rows) > max_rows:
            lines.append(f"… ({len(self.rows) - max_rows} more rows)")
        return "\n".join(lines)


def fetch_pdf_bytes(
    url: str,
    user_agent: str = _UA,
    timeout: int = _REQUEST_TIMEOUT,
) -> bytes:
    """GET a PDF; raise on non-2xx."""
    r = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    r.raise_for_status()
    return r.content


def _caption_above(page, bbox, band: float = 46.0) -> str:
    """Nearest non-empty text line in the strip directly above a table's bbox.

    `band` (~2-3 lines at typical supplement font sizes) is the lookback height
    in PDF points; the line closest to the table top wins. Fails soft to '' on
    any crop/geometry edge case (table flush to page top, malformed bbox)."""
    x0, top, x1, _bottom = bbox
    if top <= 1:
        return ""
    try:
        text = page.crop((x0, max(0.0, top - band), x1, top)).extract_text() or ""
    except Exception:  # noqa: BLE001 — geometry/crop edge cases, never fatal
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def extract_tables(pdf_bytes: bytes) -> list[Table]:
    """Return every non-empty table from every page, each tagged with the
    caption line above it. Uses `find_tables()` (not `extract_tables()`) so each
    table's bbox is available to locate that caption."""
    out: list[Table] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            for tbl in page.find_tables():
                grid = tbl.extract() or []
                if len(grid) < 2:
                    continue
                header = [_norm(c) for c in grid[0]]
                if not any(h.strip() for h in header):
                    continue
                rows = [[_norm(c) for c in row] for row in grid[1:]]
                out.append(Table(page=i, header=header, rows=rows,
                                 caption=_caption_above(page, tbl.bbox)))
    return out


def find_tables(tables: list[Table], header_patterns: Iterable[str]) -> list[Table]:
    """Filter `tables` to those whose header matches any of `header_patterns`."""
    pats = list(header_patterns)
    return [t for t in tables if t.header_matches(pats)]


def extract_text_pages(pdf_bytes: bytes) -> list[str]:
    """Per-page plain text (one string per page, 1-indexed by list position+1).

    For text-layout parsers that the grid table detector can't serve. GAAP
    ASC 944 loss-development triangles in 10-Ks are *borderless* — `find_tables()`
    collapses them into a single wide column blob — but their text layer is clean
    and column-aligned, so `parse.triangles.parse_development_text` reconstructs
    the triangles from this instead. Cheap relative to `extract_tables` (no table
    detection), so callers can run both passes over one PDF."""
    out: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return out


def _norm(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\n", " ").strip())
