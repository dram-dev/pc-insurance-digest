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

    def header_matches(self, patterns: Iterable[str]) -> bool:
        """True if the joined header text matches any regex in `patterns`."""
        joined = " ".join(c.lower() for c in self.header)
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


def extract_tables(pdf_bytes: bytes) -> list[Table]:
    """Return every non-empty table from every page."""
    out: list[Table] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            for tbl in page.extract_tables() or []:
                if not tbl or len(tbl) < 2:
                    continue
                header = [_norm(c) for c in tbl[0]]
                rows = [[_norm(c) for c in row] for row in tbl[1:]]
                if not any(h.strip() for h in header):
                    continue
                out.append(Table(page=i, header=header, rows=rows))
    return out


def find_tables(tables: list[Table], header_patterns: Iterable[str]) -> list[Table]:
    """Filter `tables` to those whose header matches any of `header_patterns`."""
    pats = list(header_patterns)
    return [t for t in tables if t.header_matches(pats)]


def _norm(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\n", " ").strip())
