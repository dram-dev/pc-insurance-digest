"""CourtListener / RECAP federal docket ingestor.

Pulls new federal docket filings from the free PACER mirror maintained by
the Free Law Project. Targets P&C-relevant MDL courts and filters to
nature-of-suit codes associated with mass-tort, product-liability, and
property-damage litigation that drives social inflation.

Rate limits (free tier): 5 req/min, 50 req/hour, 125 req/day.
This module enforces a 12s inter-request sleep and a 100-request daily
cap via a module-level counter. Courts are queried in tier order: tier1
(highest verdict volume) → emerging → tier3. Federal circuits are skipped
(appellate dockets are less useful for new-filing tracking).

Token absent → no-op. Source multiplier treated as 1.0 (no explicit
entry in CLAUDE.md; will be calibrated post-launch).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.dates import parse_date

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "courtlistener_courts.yaml"
_API_BASE = "https://www.courtlistener.com/api/rest/v4"
_REQUEST_TIMEOUT = 20
_SLEEP_BETWEEN_CALLS = 12   # 5 req/min cap
_DAILY_REQUEST_CAP = 100    # stay under 125/day; leave headroom for retries

# Nature-of-suit codes that map to P&C-relevant dockets. Expanded 2026-05-25
# from {365,360,385,480,870} to include auto liability (350/355), med-mal
# (362), pharma mass torts (367), asbestos (368 — major long-tail P&C
# exposure), and other property damage (380).
_PC_RELEVANT_NOS = frozenset({
    "350",  # Motor Vehicle — auto liability suits
    "355",  # Motor Vehicle Product Liability — auto manufacturer defects
    "360",  # Other Personal Injury
    "362",  # Personal Injury — Medical Malpractice
    "365",  # Personal Injury — Product Liability
    "367",  # Health Care / Pharmaceutical Personal Injury Product Liability
    "368",  # Asbestos Personal Injury Product Liability — long-tail P&C
    "380",  # Other Personal Property Damage
    "385",  # Property Damage — Product Liability
    "480",  # Consumer Credit
    "870",  # Tax (insurer tax disputes)
})

# Lookback window: query dockets filed in the last N days.
_FILED_AFTER_DAYS = 2

# Module-level counter — reset between process restarts (daily launchd runs).
_request_count = 0


def _is_pc_relevant(docket: dict) -> bool:
    """True if the docket's nature-of-suit code is in our P&C list."""
    nos = str(docket.get("nature_of_suit") or "")
    # CourtListener may return the code as an integer or string; also try the
    # nos field as a numeric code extracted from a label like "365: Product Liability".
    if nos in _PC_RELEVANT_NOS:
        return True
    # Sometimes the API returns a string like "365" or an integer
    try:
        return str(int(nos)) in _PC_RELEVANT_NOS
    except (ValueError, TypeError):
        pass
    # Fall back: scan for numeric code embedded in label string
    for code in _PC_RELEVANT_NOS:
        if nos.startswith(code):
            return True
    return False


def _match_mdl_keyword(case_name: str, keywords: list[str]) -> str | None:
    """Substring-match case_name (case-insensitive) against MDL keywords.

    Returns the first matching keyword (as configured) or None. Case names
    are formal legal text — substring match has low false-positive rate.
    """
    if not case_name or not keywords:
        return None
    name_lower = case_name.lower()
    for kw in keywords:
        if kw.lower() in name_lower:
            return kw
    return None


class CourtListenerIngestor(IngestorBase):
    name = "courtlistener"

    def __init__(self) -> None:
        if not settings.courtlistener_token:
            logger.info("courtlistener: COURTLISTENER_TOKEN not set; ingestor will no-op")
            self.enabled = False
            return
        self.enabled = True
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"CourtListener courts config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())
        self.mdl_keywords: list[str] = self.config.get("mdl_keywords") or []

    def fetch(self) -> list[IngestedItem]:
        if not self.enabled:
            return []

        global _request_count

        headers = {"Authorization": f"Token {settings.courtlistener_token}"}
        filed_after = (datetime.now(tz=timezone.utc) - timedelta(days=_FILED_AFTER_DAYS)).date().isoformat()
        items: list[IngestedItem] = []

        # Query tier1 + emerging; skip tier3 + federal_circuits unless budget allows.
        for tier in ("tier1", "emerging", "tier3"):
            if _request_count >= _DAILY_REQUEST_CAP:
                logger.warning(
                    "courtlistener: daily request cap (%d) reached; stopping", _DAILY_REQUEST_CAP
                )
                break
            for court_id in self.config.get(tier, []):
                if _request_count >= _DAILY_REQUEST_CAP:
                    break
                params = {
                    "court":       court_id,
                    "filed_after": filed_after,
                    "order_by":    "-date_filed",
                    "page_size":   20,
                }
                try:
                    r = requests.get(
                        f"{_API_BASE}/dockets/",
                        headers=headers,
                        params=params,
                        timeout=_REQUEST_TIMEOUT,
                    )
                    r.raise_for_status()
                    _request_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("courtlistener: %s fetch failed: %s", court_id, exc)
                    _request_count += 1
                    time.sleep(_SLEEP_BETWEEN_CALLS)
                    continue

                for docket in r.json().get("results", []):
                    if not _is_pc_relevant(docket):
                        continue
                    abs_url = docket.get("absolute_url")
                    full_url = urljoin("https://www.courtlistener.com", abs_url) if abs_url else None
                    case_name = docket.get("case_name") or f"Docket {docket.get('id', '?')}"
                    mdl_match = _match_mdl_keyword(case_name, self.mdl_keywords)
                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=str(docket["id"]),
                            title=f"[{court_id.upper()}] {case_name}",
                            url=full_url,
                            author=court_id.upper(),
                            published_at=parse_date(docket.get("date_filed")),
                            metadata={
                                "topic_hint":     "social_inflation",
                                "court":          court_id,
                                "tier":           tier,
                                "docket_id":      docket.get("id"),
                                "nature_of_suit": docket.get("nature_of_suit"),
                                "date_filed":     docket.get("date_filed"),
                                "docket_number":  docket.get("docket_number"),
                                "mdl_match":      mdl_match,   # None unless case_name hit a keyword
                            },
                        )
                    )

                logger.debug(
                    "courtlistener: court=%s filed_after=%s results=%d requests_used=%d",
                    court_id, filed_after, len(r.json().get("results", [])), _request_count,
                )
                time.sleep(_SLEEP_BETWEEN_CALLS)

        logger.info(
            "courtlistener: fetch complete — %d items, %d API requests used",
            len(items), _request_count,
        )
        return items
