"""SERFF rate filings ingestor.

Pulls state DOI rate filings from SERFF Filing Access (or each state's
custom equivalent) and emits items with requested-rate-change >= the
configured threshold (default 5%). Items are auto-kept into
`regulatory_rate` by `auto_keep_serff()`.

Portal types (dispatched in `_scrape_state`):
  - serff_standard      → https://filingaccess.serff.com/sfa/home/<STATE>
                          (used by TX, NY, LA and ~30 other states)
  - cdi_prior_approval  → CA's own front-end to SERFF data
  - floir_irfa          → FL's Internet Rate File Access

Operational notes:
  - SERFF Filing Access typically requires a POST with search criteria
    (closure_status=Closed-Approved, filed_after=<ISO>, filing_type=Rate)
    rather than a naive GET. Wire that per-portal here as states are
    validated. The scaffold currently does a permissive GET + CSS
    selector parse; states stay enabled:false until the per-state
    behavior is confirmed on the Mac mini.
  - The ≥5% requested-change filter is applied post-fetch from the
    parsed value. Some portals expose this as a percentage; others as
    a basis-point integer. `_parse_rate_change` normalizes both.

Per the plan, build order is CA → FL → TX → NY → LA. All states ship
disabled. Flip per-state once the selector is confirmed.

Wave 3 Phase 2 — Liability + regulatory parallel track.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from digest.ingest.base import IngestedItem, IngestorBase
from digest.parse.dates import parse_date

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "serff_states.yaml"

# Match "12.5%", "+12.5%", "-3%", "1250 bps" — return float percentage.
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
_BPS_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*bps", re.IGNORECASE)


def _parse_rate_change(text: str) -> float | None:
    """Parse a requested rate change value as percent. None if not parseable.

    Handles "12.5%", "+12.5%", "-3%", "1250 bps" (basis points → 12.5%).
    """
    if not text:
        return None
    m = _PCT_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _BPS_RE.search(text)
    if m:
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            return None
    return None


def _lob_matches(text: str, watched: list[str]) -> bool:
    if not text or not watched:
        return False
    lower = text.lower()
    return any(lob.lower() in lower for lob in watched)


class SerffIngestor(IngestorBase):
    name = "serff"

    def __init__(self) -> None:
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"serff config missing: {_CONFIG_PATH}")
        config = yaml.safe_load(_CONFIG_PATH.read_text())
        self.defaults: dict[str, Any] = config.get("defaults", {}) or {}
        self.states: list[dict[str, Any]] = config.get("states", []) or []
        self.min_pct: float = float(self.defaults.get("min_rate_change_pct", 5.0))
        self.lookback_days: int = int(self.defaults.get("lookback_days", 30))
        self.lobs: list[str] = list(self.defaults.get("lines_of_business") or [])
        self.user_agent: str = self.defaults.get("user_agent") or "Mozilla/5.0"
        self.timeout: int = int(self.defaults.get("request_timeout", 25))

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        enabled = [s for s in self.states if s.get("enabled", False)]
        if not enabled:
            logger.info(
                "serff: no states enabled — validate selectors on Mac mini and "
                "flip enabled:true per state in config/serff_states.yaml"
            )
            return items
        for entry in enabled:
            state = entry.get("state", "?")
            try:
                items.extend(self._scrape_state(entry))
            except Exception as exc:  # noqa: BLE001
                logger.warning("serff: %s scrape failed: %s", state, exc)
        logger.info(
            "serff: %d filings emitted from %d enabled states", len(items), len(enabled)
        )
        return items

    # ── Per-state dispatch ────────────────────────────────────────────────

    def _scrape_state(self, entry: dict[str, Any]) -> list[IngestedItem]:
        portal = entry.get("portal", "serff_standard")
        state = entry["state"]
        url = entry["url_template"]
        selectors = entry.get("selectors") or {}
        agency = entry.get("agency", "")

        # Portal-specific fetch (some need POST + search params).
        if portal == "serff_standard":
            html = self._fetch_serff_standard(state, url)
        elif portal == "cdi_prior_approval":
            html = self._fetch_cdi_prior_approval(state, url)
        elif portal == "floir_irfa":
            html = self._fetch_floir_irfa(state, url)
        else:
            logger.warning("serff: %s — unknown portal %r; skipping", state, portal)
            return []

        if not html:
            return []
        return self._parse_filings(state, agency, url, html, selectors)

    def _fetch_serff_standard(self, state: str, url: str) -> str:
        """Standard SERFF Filing Access. TODO: switch to POST + search criteria
        (closure_status=Closed-Approved, filed_after=<lookback>, filing_type=Rate).
        Naive GET returns the landing page; state stays disabled until this is
        replaced with the form POST and validated."""
        r = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def _fetch_cdi_prior_approval(self, state: str, url: str) -> str:
        """CA CDI Prior Approval search. TODO: this is an Oracle Apex form;
        likely needs session bootstrapping + POST. Stays disabled until
        captured properly."""
        r = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def _fetch_floir_irfa(self, state: str, url: str) -> str:
        """FLOIR iRFA. TODO: ASP.NET form with viewstate + event target.
        Stays disabled until captured properly."""
        r = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    # ── HTML → IngestedItem ───────────────────────────────────────────────

    def _parse_filings(
        self,
        state: str,
        agency: str,
        base_url: str,
        html: str,
        selectors: dict[str, str],
    ) -> list[IngestedItem]:
        soup = BeautifulSoup(html, "html.parser")
        row_sel = selectors.get("row", "")
        if not row_sel:
            logger.warning("serff: %s — no row selector configured", state)
            return []
        rows = soup.select(row_sel)
        if not rows:
            logger.warning(
                "serff: %s — selector %r returned 0 rows. Inspect the live page "
                "and update config/serff_states.yaml.",
                state, row_sel,
            )
            return []

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.lookback_days)
        items: list[IngestedItem] = []
        seen_ids: set[str] = set()
        for node in rows:
            company    = _text(node, selectors.get("company"))
            rate_text  = _text(node, selectors.get("rate_change"))
            lob_text   = _text(node, selectors.get("lob"))
            filing_id  = _text(node, selectors.get("filing_id")) or _text_attr(node, "data-filing")
            status     = _text(node, selectors.get("status"))
            filed_text = _text(node, selectors.get("filed_at"))

            # Filter: LOB must match a watched line.
            if self.lobs and not _lob_matches(lob_text, self.lobs):
                continue

            # Filter: rate change must be >= threshold (absolute value of % change).
            rate_pct = _parse_rate_change(rate_text)
            if rate_pct is None or abs(rate_pct) < self.min_pct:
                continue

            # Filter: filing within lookback window (if parseable).
            filed_at = parse_date(filed_text)
            if filed_at and filed_at < cutoff:
                continue

            if not filing_id:
                continue
            if filing_id in seen_ids:
                continue
            seen_ids.add(filing_id)

            # Prefer an explicit <a href>; otherwise reuse the base URL.
            a = node.select_one("a[href]")
            href = a.get("href") if a else ""
            if href and not href.startswith("http"):
                href = urljoin(base_url, href)

            title = f"[{state} DOI] {company or 'Filing'} — {rate_pct:+.1f}% on {lob_text or 'rate filing'}"
            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=f"{state}:{filing_id}",
                    title=title,
                    url=href or base_url,
                    author=agency,
                    published_at=filed_at,
                    metadata={
                        "topic_hint":   "regulatory_rate",
                        "state":        state,
                        "agency":       agency,
                        "company":      company,
                        "lob":          lob_text,
                        "filing_id":    filing_id,
                        "rate_change_pct": rate_pct,
                        "status":       status,
                    },
                )
            )
        logger.info("serff: %s — %d filings ≥%.1f%%", state, len(items), self.min_pct)
        return items


def _text(node: Any, selector: str | None) -> str:
    if not selector:
        return ""
    el = node.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _text_attr(node: Any, attr: str) -> str:
    val = node.get(attr)
    return str(val).strip() if val else ""
