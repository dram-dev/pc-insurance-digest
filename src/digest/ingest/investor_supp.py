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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.pdf_tables import (
    Table,
    extract_tables,
    fetch_pdf_bytes,
    find_tables,
)

logger = logging.getLogger(__name__)

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
        paid = find_tables(tables, self.paid_patterns)
        pending = find_tables(tables, self.pending_patterns)

        out: list[IngestedItem] = []
        out.extend(self._tables_to_items(paid, "paid_severity", ticker, name, year, quarter, url))
        out.extend(self._tables_to_items(pending, "pending_count", ticker, name, year, quarter, url))
        logger.info(
            "investor_supp: %s Q%d %d — paid=%d pending=%d total_tables=%d",
            ticker, quarter, year, len(paid), len(pending), len(tables),
        )
        return out

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
