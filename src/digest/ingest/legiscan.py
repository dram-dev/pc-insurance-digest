"""LegiScan state-legislation ingestor (Lead 9 — Regulatory Burden Barometer).

Per-state full-text search (LegiScan `getSearch`) for insurance bills. Bills with
recent legislative action surface as `regulatory_rate` items; `auto_keep_legiscan`
(db.py) keeps them and stamps `items.state` from the bill's state, so they feed
the per-state burden velocity in `db.burden_by_state()` / `digest burden`.

LEGISCAN_API_KEY absent → no-op (like the courtlistener ingestor). Free tier is
30k queries/month; this issues one getSearch per configured state per run.

Results come back relevance-sorted; we keep only bills whose `last_action_date`
is within `recency_days` (the velocity signal — recently-active legislation) and
cap at `max_per_state`. Bill detail (sponsors, full text, roll calls) is left to
a future getBill enrichment; the search row already carries number/title/action.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.dates import parse_date

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "legiscan.yaml"
_API_URL = "https://api.legiscan.com/"
_REQUEST_TIMEOUT = 30


class LegiScanIngestor(IngestorBase):
    name = "legiscan"

    def __init__(self) -> None:
        if not settings.legiscan_api_key:
            logger.info("legiscan: LEGISCAN_API_KEY not set; ingestor will no-op")
            self.enabled = False
            return
        self.enabled = True
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"legiscan config missing: {_CONFIG_PATH}")
        cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        d = cfg.get("defaults", {}) or {}
        self.query: str = d.get("query", "insurance")
        self.year: int = int(d.get("year", 2))
        self.recency_days: int = int(d.get("recency_days", 30))
        self.max_per_state: int = int(d.get("max_per_state", 12))
        self.states: list[str] = cfg.get("states", []) or []

    def fetch(self) -> list[IngestedItem]:
        if not self.enabled:
            return []
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=self.recency_days)).date().isoformat()
        items: list[IngestedItem] = []
        for state in self.states:
            try:
                items.extend(self._search_state(state, cutoff))
            except Exception as exc:  # noqa: BLE001
                logger.warning("legiscan: %s search failed: %s", state, exc)
        logger.info("legiscan: %d recent insurance bills across %d states",
                    len(items), len(self.states))
        return items

    def _search_state(self, state: str, cutoff: str) -> list[IngestedItem]:
        r = requests.get(_API_URL, params={
            "key":   settings.legiscan_api_key,
            "op":    "getSearch",
            "state": state,
            "query": self.query,
            "year":  self.year,
        }, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "OK":
            logger.warning("legiscan: %s getSearch status=%s", state, j.get("status"))
            return []

        # searchresult is {"summary": {...}, "0": {...}, "1": {...}, …}.
        searchresult = j.get("searchresult", {}) or {}
        results = [v for k, v in searchresult.items()
                   if k != "summary" and isinstance(v, dict)]

        out: list[IngestedItem] = []
        for b in results:
            last_action_date = b.get("last_action_date") or ""
            if last_action_date < cutoff:        # ISO dates compare lexically
                continue
            bill_id = b.get("bill_id")
            if bill_id is None:
                continue
            number = b.get("bill_number") or str(bill_id)
            title = b.get("title") or number
            out.append(IngestedItem(
                source=self.name,
                source_id=str(bill_id),
                title=f"[{state} {number}] {title}",
                url=b.get("url") or b.get("state_link"),
                author=f"{state} Legislature",
                content=b.get("last_action") or "",
                published_at=parse_date(last_action_date),
                metadata={
                    "topic_hint":       "regulatory_rate",
                    "state":            state,
                    "bill_number":      number,
                    "last_action":      b.get("last_action"),
                    "last_action_date": last_action_date,
                    "relevance":        b.get("relevance"),
                },
            ))
            if len(out) >= self.max_per_state:
                break
        logger.info("legiscan: %s — %d recent insurance bills", state, len(out))
        return out
