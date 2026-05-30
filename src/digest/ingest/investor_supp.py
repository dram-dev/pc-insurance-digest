"""Insurer investor-supplement PDF ingestor.

Quarterly cadence. Fetches the financial-supplement PDF that each
public insurer posts to its IR site after earnings release, extracts
tables matching paid-severity and pending-count header patterns, and
emits one IngestedItem per matched table.

Items land in the `reserving` topic via `auto_keep_investor_supp` (db.py).
Stub-summarize path in summarize.py renders the table as deterministic
text so MLX never hallucinates over a structured disclosure.

Each insurer is `enabled: false` in config/investor_supplements.yaml
until the URL template is confirmed against a live quarterly PDF on
the Mac mini.

Wave 3 Phase 3 — Liability Intelligence cluster.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest import db
from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.pdf_tables import (
    Table,
    extract_tables,
    fetch_pdf_bytes,
    find_tables,
)
from digest.parse.triangles import parse_triangle

logger = logging.getLogger(__name__)

# Last day of each calendar quarter, for the triangle snapshot key (as_of).
_QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}

# Line-of-business detection from a triangle's header text. First match wins;
# unmatched triangles fall back to 'all_lines'.
_LOB_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"personal\s+auto|private\s+passenger", "personal_auto"),
    (r"commercial\s+auto", "commercial_auto"),
    (r"\bauto(?:mobile)?\b", "auto"),
    (r"homeowners?|\bhome\b", "homeowners"),
    (r"workers'?\s+comp", "workers_comp"),
    (r"general\s+liability|\bGL\b", "general_liability"),
    (r"commercial\s+property", "commercial_property"),
    (r"\bproperty\b", "property"),
    (r"\bumbrella\b", "umbrella"),
)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "investor_supplements.yaml"


def _current_quarter(now: datetime | None = None) -> tuple[int, int]:
    """Return (year, quarter_number) for the most recent completed quarter.

    Backs up two calendar months from today so we're requesting a
    supplement that's likely already posted (most carriers release Qn
    supplements within ~6 weeks of quarter-end).
    """
    now = now or datetime.now(tz=timezone.utc)
    target = now.replace(day=1)
    for _ in range(2):
        prev_month = target.month - 1 or 12
        prev_year = target.year - (1 if target.month == 1 else 0)
        target = target.replace(year=prev_year, month=prev_month)
    quarter = (target.month - 1) // 3 + 1
    return target.year, quarter


class InvestorSuppIngestor(IngestorBase):
    name = "investor_supp"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"investor_supp config missing: {_CONFIG_PATH}")
        cfg = yaml.safe_load(_CONFIG_PATH.read_text())
        self.defaults: dict[str, Any] = cfg.get("defaults", {}) or {}
        self.insurers: list[dict[str, Any]] = cfg.get("insurers", []) or []
        self.user_agent: str = self.defaults.get("user_agent") or "Mozilla/5.0"
        self.timeout: int = int(self.defaults.get("request_timeout", 30))
        self.paid_patterns: list[str] = list(
            self.defaults.get("paid_severity_header_patterns") or []
        )
        self.pending_patterns: list[str] = list(
            self.defaults.get("pending_count_header_patterns") or []
        )
        self.triangle_patterns: list[str] = list(
            self.defaults.get("triangle_header_patterns") or []
        )

    def fetch(self) -> list[IngestedItem]:
        enabled = [i for i in self.insurers if i.get("enabled", False)]
        if not enabled:
            logger.info(
                "investor_supp: no insurers enabled — validate URL templates on "
                "Mac mini and flip enabled:true per insurer in "
                "config/investor_supplements.yaml"
            )
            return []
        year, quarter = _current_quarter()
        items: list[IngestedItem] = []
        for entry in enabled:
            ticker = entry["ticker"]
            try:
                items.extend(self._scrape_insurer(entry, year, quarter))
            except Exception as exc:  # noqa: BLE001
                logger.warning("investor_supp: %s fetch failed: %s", ticker, exc)
        logger.info(
            "investor_supp: %d table-items emitted (%d insurers enabled, %dQ%d)",
            len(items), len(enabled), year, quarter,
        )
        return items

    def _scrape_insurer(
        self,
        entry: dict[str, Any],
        year: int,
        quarter: int,
    ) -> list[IngestedItem]:
        ticker = entry["ticker"]
        name = entry.get("name", ticker)
        url = entry["url_template"].format(year=year, q=quarter, quarter=f"Q{quarter}")

        pdf = fetch_pdf_bytes(url, user_agent=self.user_agent, timeout=self.timeout)
        tables = extract_tables(pdf)

        # Loss-development triangles (Lead 6) are structured into the triangle
        # store, not emitted as news items. Pull them out first so they don't
        # double-count down the paid/pending news path.
        triangle_tables = find_tables(tables, self.triangle_patterns) if self.triangle_patterns else []
        cells_written = self._route_triangles(triangle_tables, ticker, year, quarter)
        triangle_ids = {id(t) for t in triangle_tables}
        remaining = [t for t in tables if id(t) not in triangle_ids]

        paid = find_tables(remaining, self.paid_patterns)
        pending = find_tables(remaining, self.pending_patterns)

        out: list[IngestedItem] = []
        out.extend(self._tables_to_items(paid, "paid_severity", ticker, name, year, quarter, url))
        out.extend(self._tables_to_items(pending, "pending_count", ticker, name, year, quarter, url))
        logger.info(
            "investor_supp: %s Q%d %d — paid=%d pending=%d triangles=%d (cells=%d) total_tables=%d",
            ticker, quarter, year, len(paid), len(pending),
            len(triangle_tables), cells_written, len(tables),
        )
        return out

    def _route_triangles(
        self,
        tables: list[Table],
        ticker: str,
        year: int,
        quarter: int,
    ) -> int:
        """Parse triangle-shaped tables → upsert loss-triangle cells. Returns the
        number of cells written across all tables (0 when none parse cleanly).

        Guards the loss_triangles PK against collisions: if two triangles in one
        supplement resolve to the same (lob, metric) — e.g. both fall back to
        'all_lines' because their LOB wasn't detectable — the later ones get a
        numeric suffix so they don't silently overwrite each other via the
        INSERT OR REPLACE upsert."""
        as_of = f"{year}-{_QUARTER_END[quarter]}"
        total = 0
        seen: dict[tuple[str, str], int] = {}
        for t in tables:
            metric = _detect_metric(t)
            lob = _detect_lob(t)
            seen[(lob, metric)] = seen.get((lob, metric), 0) + 1
            if seen[(lob, metric)] > 1:
                lob = f"{lob}_{seen[(lob, metric)]}"
            cells = parse_triangle(
                t, insurer=ticker, lob=lob, metric=metric, as_of=as_of,
            )
            if cells:
                total += db.upsert_triangle_cells(cells)
        return total

    def _tables_to_items(
        self,
        tables: list[Table],
        table_type: str,
        ticker: str,
        name: str,
        year: int,
        quarter: int,
        url: str,
    ) -> list[IngestedItem]:
        out: list[IngestedItem] = []
        for t in tables:
            out.append(
                IngestedItem(
                    source=self.name,
                    source_id=f"{ticker}-{year}-Q{quarter}-{table_type}-p{t.page}",
                    title=f"[{ticker}] Q{quarter} {year} {table_type.replace('_', ' ')}",
                    url=url,
                    author=name,
                    content=t.to_text(),
                    metadata={
                        "topic_hint":   "reserving",
                        "ticker":       ticker,
                        "name":         name,
                        "year":         year,
                        "quarter":      quarter,
                        "table_type":   table_type,
                        "table_header": t.header,
                        "table_rows":   len(t.rows),
                        "table_page":   t.page,
                    },
                )
            )
        return out


def _detect_metric(table: Table) -> str:
    """'paid' | 'incurred' from a triangle's caption + header. Defaults to
    'incurred' (the more commonly disclosed development basis) when ambiguous.
    Scans `search_text` because the basis is usually in the caption, not the
    column-header row."""
    joined = table.search_text.lower()
    if "paid" in joined and "incurred" not in joined:
        return "paid"
    return "incurred"


def _detect_lob(table: Table) -> str:
    """Line of business from a triangle's caption + header; 'all_lines' fallback.
    Scans `search_text` because the LOB is usually in the caption, not the
    column-header row."""
    joined = table.search_text.lower()
    for pattern, lob in _LOB_PATTERNS:
        if re.search(pattern, joined, re.IGNORECASE):
            return lob
    return "all_lines"
