"""SEC EDGAR ingestor — recent filings for the P&C insurer ticker universe.

Uses the public EDGAR submissions API (no auth). SEC requires a user-agent with
a real name and email; set EDGAR_USER_AGENT in .env.

Coverage & content policy (Wave 4 fix):
  * Filings are selected per-FORM (the most recent N of each relevant form), not
    by scanning a flat window of the latest filings. A chatty filer (Progressive
    files monthly 8-Ks + many Form 4s) used to push its annual 10-K past the flat
    scan window and out of the digest entirely; per-form selection guarantees the
    latest 10-K/10-Q are always captured.
  * Body content is fetched only for filings NOT already stored (DB-aware) and
    within a per-form age cap (10-K up to ~13 months, 10-Q ~5 months, 8-K ~1
    month). So a 10-K first seen 2-3 months after its filing date still arrives
    WITH content (the old flat 21-day cutoff left it empty), but content is not
    re-downloaded on later runs.
  * For 8-K we fetch the EX-99.1 press release; for 10-Q/10-K the primary-doc head
    PLUS extracted MD&A windows — financial highlights (combined ratio, premiums,
    net income) and the reserve-development discussion — since the headline
    figures and the loss-reserve note live deep in the document, not in the head.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from digest import db
from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.edgar import fetch_8k_content, fetch_html_text

logger = logging.getLogger(__name__)

# Lead 5 (Disclosure Sentiment): the 10-K/10-Q *head* (financial highlights) rarely
# carries reserve tone — that lives in the loss-reserve note / MD&A, deep in the
# doc. Pull windows around reserve-discussion markers so disclosure.score_filing
# has the language to read. The HTML download is already full (only the extracted
# *text* is capped), so retaining more text + slicing here costs no extra fetch.
_RESERVE_MARKERS = re.compile(
    r"prior[\s-]*year(?:'s)?\s+(?:reserve\s+)?development"
    r"|loss(?:es)?\s+and\s+loss\s+adjustment\s+expense"
    r"|reserves?\s+for\s+(?:unpaid\s+)?(?:losses|claims)"
    r"|incurred\s+but\s+not\s+reported|\bIBNR\b"
    r"|(?:favorable|unfavorable|adverse)\s+(?:prior[\s-]*year\s+)?development"
    r"|reserve\s+(?:strengthening|releases?|deficienc)",
    re.IGNORECASE,
)
# Financial-highlights markers — the underwriting figures the head misses (they
# live in MD&A / results of operations). Windows require a nearby digit so a
# label-only mention in the business description doesn't crowd out the results.
_FINANCIAL_MARKERS = re.compile(
    r"combined ratio"
    r"|net premiums (?:written|earned)"
    r"|underwriting (?:profit|margin|income)"
    r"|policies in force"
    r"|loss(?:es)?\s+and\s+loss[\s-]adjustment\s+expense\s+ratio"
    r"|\bnet income\b",
    re.IGNORECASE,
)
# Output text retained from a 10-K/10-Q so the reserve note (often >60% through a
# 10-K) is within reach; the underlying HTML is downloaded in full regardless.
_DEEP_FETCH_CHARS = 800_000
_HEAD_CHARS = 5000
# EX-99.1 press releases carry the combined-ratio / development tables well past
# 5K chars (e.g. Progressive's monthly release), so fetch a generous slice.
_8K_CONTENT_CHARS = 20_000


def _excerpt(text: str, markers: re.Pattern, window: int = 1400,
             max_total: int = 3000, require_digit: bool = False) -> str:
    """Concatenate text windows around `markers`, deduped and capped.

    Windows are bucketed by start position so overlapping matches don't repeat,
    and (when require_digit) windows without a number are skipped — that keeps a
    label-only mention ('combined ratio' in a heading) from displacing the actual
    results table. Returns '' when nothing matches (so nothing is appended)."""
    if not text:
        return ""
    chunks: list[str] = []
    used = 0
    seen: set[int] = set()
    for m in markers.finditer(text):
        start = max(0, m.start() - window // 3)
        bucket = start // 600
        if bucket in seen:
            continue
        chunk = text[start:m.start() + window]
        if require_digit and not re.search(r"\d", chunk):
            continue
        seen.add(bucket)
        chunks.append(chunk)
        used += len(chunk)
        if used >= max_total:
            break
    return " … ".join(chunks)[:max_total]


def _reserve_excerpt(text: str, window: int = 1400, max_total: int = 3000) -> str:
    """Windows around loss-reserve / prior-year-development language (Lead 5)."""
    return _excerpt(text, _RESERVE_MARKERS, window, max_total)


def _financial_excerpt(text: str, window: int = 1200, max_total: int = 4000) -> str:
    """Windows around underwriting-result language (combined ratio, premiums, net
    income), requiring a nearby digit so results — not labels — are captured."""
    return _excerpt(text, _FINANCIAL_MARKERS, window, max_total, require_digit=True)


EDGAR_CONFIG = Path(__file__).resolve().parents[3] / "config" / "edgar_tickers.yaml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"

# Filing types we care about
RELEVANT_FORMS = {"10-K", "10-Q", "8-K", "13F-HR"}

# How many of each form to keep (most recent first). Per-form so a high filing
# cadence on one form can't bury the others — the annual 10-K is always retained.
_MAX_PER_FORM = {"10-K": 1, "10-Q": 2, "8-K": 8, "13F-HR": 2}

# Forms that get full content fetched. 13F-HR is XML holdings, parsed differently.
_FETCH_CONTENT_FORMS = {"8-K", "10-Q", "10-K"}

# Per-form max age (days) for fetching body content. Annual 10-Ks are first seen
# ~2-3 months after the fiscal year and we may catch them later still, so the cap
# is generous; 8-Ks are point-in-time so a short cap suffices. Combined with the
# DB-aware "new filings only" check, this fetches a fresh 10-K's body once.
_CONTENT_MAX_AGE_DAYS = {"10-K": 400, "10-Q": 150, "8-K": 31}

# Insurer (non-fund) filings — locked topic_hint per form so the summarizer
# doesn't reclassify a bare 10-Q stub as ai_insurtech. Matches the SQL-level
# topic lock in db.auto_keep_insurer_filings.
_INSURER_TOPIC_HINT = {
    "8-K":    "underwriting_results",
    "10-Q":   "underwriting_results",
    "10-K":   "underwriting_results",
    "13F-HR": "ma_capital",
}


def _select_filings(recent: dict, is_fund: bool) -> list[dict]:
    """Pick the most recent `_MAX_PER_FORM` filings of each relevant form.

    `recent` is EDGAR's filings.recent object (parallel arrays). Iterating
    per-form (rather than a flat top-N) guarantees a chatty filer's annual 10-K
    is retained even when buried behind months of 8-Ks and Form 4s.
    """
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    by_form: dict[str, list[dict]] = {}
    for i, form in enumerate(forms):
        if form not in RELEVANT_FORMS:
            continue
        if form == "13F-HR" and not is_fund:
            continue
        by_form.setdefault(form, []).append({
            "form": form,
            "filing_date": dates[i],
            "accession": accessions[i],
            "primary_doc": primary_docs[i],
        })
    selected: list[dict] = []
    for form, lst in by_form.items():
        lst.sort(key=lambda d: d["filing_date"], reverse=True)
        selected.extend(lst[: _MAX_PER_FORM.get(form, 4)])
    return selected


def _content_age_ok(form: str, published: datetime | None, now: datetime) -> bool:
    """True if `form` filed at `published` is within its content-fetch age cap."""
    cap = _CONTENT_MAX_AGE_DAYS.get(form)
    if cap is None or published is None:
        return False
    return (now - published).days <= cap


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

    def _fetch_content(self, form: str, cik_int: str, accession: str, url: str) -> str | None:
        """Body content for a filing: 8-K → EX-99.1 release; 10-Q/10-K → head +
        financial-highlights + reserve-discussion excerpts."""
        if form == "8-K":
            return fetch_8k_content(
                cik_int, accession, self.headers, max_chars=_8K_CONTENT_CHARS
            )
        full = fetch_html_text(url, self.headers, max_chars=_DEEP_FETCH_CHARS)
        if not full:
            return None
        content = full[:_HEAD_CHARS]
        fin = _financial_excerpt(full)
        if fin:
            content += "\n\n[Financial highlights]\n" + fin
        res = _reserve_excerpt(full)
        if res:
            content += "\n\n[Reserve discussion]\n" + res
        return content

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        now = datetime.now(timezone.utc)
        # Filings we already hold — fetch body content for new ones only, so a
        # fresh 10-K is downloaded once and never re-fetched. Degrade gracefully
        # (fetch by age alone) if the DB isn't reachable.
        try:
            seen = db.existing_source_ids(self.name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("edgar: existing_source_ids failed (%s); fetching by age only", exc)
            seen = set()

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
                recent = r.json().get("filings", {}).get("recent", {})

                for f in _select_filings(recent, is_fund):
                    form = f["form"]
                    accession = f["accession"]
                    filing_date = f["filing_date"]
                    primary_doc = f["primary_doc"]
                    cik_int = str(int(cik))
                    accession_nodashes = accession.replace("-", "")
                    source_id = f"{ticker}:{accession}"

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

                    # Fetch content only for genuinely-new filings within the
                    # per-form age cap (avoids re-downloading every run).
                    content: str | None = None
                    if (
                        form in _FETCH_CONTENT_FORMS
                        and source_id not in seen
                        and _content_age_ok(form, published, now)
                    ):
                        content = self._fetch_content(form, cik_int, accession, url)
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
                            source_id=source_id,
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
