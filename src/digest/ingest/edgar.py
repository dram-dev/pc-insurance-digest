"""SEC EDGAR ingestor — recent filings for the P&C insurer ticker universe.

Uses the public EDGAR submissions API (no auth). SEC requires a user-agent with
a real name and email; set EDGAR_USER_AGENT in .env.

For 8-K filings we fetch the EX-99.1 press release (earnings, reserve actions,
cat-loss disclosures); for 10-Q/10-K we grab the primary-doc head. The HTML/
exhibit-fetching mechanics live in `digest_core.ingest.edgar`; this shell owns
the ticker/CIK universe (config/edgar_tickers.yaml) and the per-form topic lock.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.edgar import fetch_8k_content, fetch_html_text

logger = logging.getLogger(__name__)

EDGAR_CONFIG = Path(__file__).resolve().parents[3] / "config" / "edgar_tickers.yaml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"

# Filing types we care about
RELEVANT_FORMS = {"10-K", "10-Q", "8-K", "13F-HR"}

# Only fetch content for filings filed within this many days (avoid backfilling
# entire history; 21d covers a full quarterly-filing cycle so each fresh 10-Q
# arrives with body text the summarizer can use).
_CONTENT_FETCH_MAX_AGE_DAYS = 21
# Forms that get full content fetched. 13F-HR is XML holdings, parsed differently.
_FETCH_CONTENT_FORMS = {"8-K", "10-Q", "10-K"}

# Insurer (non-fund) filings — locked topic_hint per form so the summarizer
# doesn't reclassify a bare 10-Q stub as ai_insurtech. Matches the SQL-level
# topic lock in db.auto_keep_insurer_filings.
_INSURER_TOPIC_HINT = {
    "8-K":    "underwriting_results",
    "10-Q":   "underwriting_results",
    "10-K":   "underwriting_results",
    "13F-HR": "ma_capital",
}


class EdgarIngestor(IngestorBase):
    name = "edgar"

    def __init__(self) -> None:
        if not settings.edgar_user_agent:
            raise RuntimeError(
                "EDGAR_USER_AGENT not set. SEC requires a user-agent like "
                "'Your Name your.email@example.com'."
            )
        self.headers = {"User-Agent": settings.edgar_user_agent}
        self.config = yaml.safe_load(EDGAR_CONFIG.read_text())
        self._cutoff = datetime.now(timezone.utc) - timedelta(days=_CONTENT_FETCH_MAX_AGE_DAYS)

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for entry in self.config["companies"]:
            cik = str(entry["cik"]).zfill(10)
            ticker = entry["ticker"]
            entity_name = entry.get("name", ticker)
            is_fund = entry.get("fund", False)
            try:
                r = requests.get(
                    SUBMISSIONS_URL.format(cik=cik), headers=self.headers, timeout=20
                )
                r.raise_for_status()
                data = r.json()
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                primary_docs = recent.get("primaryDocument", [])

                for i, form in enumerate(forms[:40]):
                    if form not in RELEVANT_FORMS:
                        continue
                    # 13F-HR only relevant for funds/institutions
                    if form == "13F-HR" and not is_fund:
                        continue

                    accession = accessions[i]
                    filing_date = dates[i]
                    primary_doc = primary_docs[i]
                    cik_int = str(int(cik))
                    accession_nodashes = accession.replace("-", "")

                    url = FILING_URL.format(
                        cik_int=cik_int,
                        accession_no_dashes=accession_nodashes,
                        primary_doc=primary_doc,
                    )

                    try:
                        published = datetime.strptime(filing_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        logger.debug("edgar: unparseable date %r for %s %s", filing_date, ticker, accession)
                        published = None

                    # Fetch content for recent filings only (avoid backfilling).
                    # 8-K → EX-99.1 press release; 10-Q/10-K → primary doc head
                    # (financial highlights live in the first ~5K chars).
                    content: str | None = None
                    if (
                        form in _FETCH_CONTENT_FORMS
                        and published
                        and published >= self._cutoff
                    ):
                        if form == "8-K":
                            content = fetch_8k_content(cik_int, accession, self.headers)
                        else:
                            content = fetch_html_text(url, self.headers, max_chars=5000)
                        if content:
                            logger.debug(
                                "edgar: fetched %s content for %s %s (%d chars)",
                                form, ticker, accession, len(content),
                            )

                    # NOTE: the is_fund→'fed_markets' branch is macro-ai-digest
                    # residue — 'fed_markets' is not a PC topic and no PC ticker
                    # is a fund (13F-HR is skipped above when not is_fund), so it
                    # never fires. Left as-is; retune if a fund ticker is added.
                    if is_fund:
                        topic_hint = "fed_markets"
                    else:
                        topic_hint = _INSURER_TOPIC_HINT.get(form, "underwriting_results")

                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=f"{ticker}:{accession}",
                            title=f"{ticker} {form} filed {filing_date}",
                            url=url,
                            author=entity_name,
                            content=content,
                            published_at=published,
                            metadata={
                                "ticker": ticker,
                                "cik": cik,
                                "form": form,
                                "accession": accession,
                                "primary_document": primary_doc,
                                "topic_hint": topic_hint,
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("edgar: failed on %s (CIK %s): %s", ticker, cik, exc)
        return items
