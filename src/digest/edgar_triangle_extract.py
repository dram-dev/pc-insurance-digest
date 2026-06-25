"""Fetch + locate + orchestrate the two EDGAR triangle extractors.

Network layer for digest.parse.edgar_triangles: finds a ticker's latest 10-K,
pulls the standalone XBRL instance and the rendered claims-development R-file(s),
runs both extractors, and diffs them so we can pick the extraction path on
evidence (build-both-and-compare). No DB writes yet — this is the evaluation
harness; the winning extractor wires into the reserving chain afterward.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.parse.edgar_triangles import (
    diff_triangles,
    parse_rfile_triangles,
    parse_xbrl_triangles,
)

logger = logging.getLogger(__name__)

_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_DELAY = 0.2  # SEC asks ≤10 req/s; stay polite
_TICKERS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "edgar_tickers.yaml"

# R-file ShortName must look like the development TRIANGLE, not a reconciliation
# / rollforward / segment prior-year-development summary.
_RFILE_WANT = re.compile(r"development", re.I)
_RFILE_CLAIM = re.compile(r"claim|incurred|paid|loss", re.I)
_RFILE_SKIP = re.compile(r"reconcil|roll[\s-]?forward|prior[\s-]?year", re.I)


def ticker_ciks() -> dict[str, str]:
    """{TICKER: zero-padded CIK} from the EDGAR universe config."""
    cfg = yaml.safe_load(_TICKERS_CONFIG.read_text())
    return {c["ticker"]: str(c["cik"]).zfill(10) for c in cfg["companies"]}


def _headers() -> dict:
    if not settings.edgar_user_agent:
        raise RuntimeError("EDGAR_USER_AGENT not set (SEC requires a UA).")
    return {"User-Agent": settings.edgar_user_agent}


def _get(url: str, timeout: int = 60) -> requests.Response:
    time.sleep(_DELAY)
    r = requests.get(url, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r


def _nth_10k(
    cik: str, n: int = 0
) -> tuple[str, str, str, str] | tuple[None, None, None, None]:
    """The n-th most recent 10-K's (accession_no_dashes, primary_doc, filing_date,
    report_date). n=0 is the latest annual filing, n=1 the prior year, etc.

    Returns a 4-tuple of Nones when fewer than n+1 10-Ks sit in the recent-filings
    window (the last ~2-3 annuals always do). `report_date` is the period-of-report
    (fiscal year-end) — handy for selection/logging; the loss-triangle `as_of` is
    derived independently from the XBRL fact contexts downstream."""
    rec = _get(_SUBMISSIONS.format(cik=cik)).json().get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    report_dates = rec.get("reportDate", [None] * len(forms))
    seen = 0
    for i, form in enumerate(forms):
        if form != "10-K":
            continue
        if seen == n:
            return (rec["accessionNumber"][i].replace("-", ""),
                    rec["primaryDocument"][i], rec["filingDate"][i], report_dates[i])
        seen += 1
    return None, None, None, None


def _latest_10k(cik: str) -> tuple[str, str, str] | tuple[None, None, None]:
    accession, primary, filing_date, _report = _nth_10k(cik, 0)
    return accession, primary, filing_date


def _rfile_candidates(base: str) -> list[tuple[str, str]]:
    """(filename, short_name) for every R-file that looks like a development
    triangle (handles filers that split the disclosure across several R-files)."""
    fs = _get(f"{base}/FilingSummary.xml").text
    out: list[tuple[str, str]] = []
    for rp in re.findall(r"<Report[^>]*>(.*?)</Report>", fs, re.DOTALL):
        fn = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", rp)
        sn = re.search(r"<ShortName>(.*?)</ShortName>", rp)
        if not (fn and sn):
            continue
        name = sn.group(1)
        if (_RFILE_WANT.search(name) and _RFILE_CLAIM.search(name)
                and not _RFILE_SKIP.search(name)):
            out.append((fn.group(1), name))
    return out


def fetch_instance_xml(cik: str, n: int = 0) -> tuple[str, str]:
    """(XBRL instance XML, filing_date) for a CIK's n-th most recent 10-K — the
    lean fetch the component-fact ingest needs (no R-files).

    n=0 is the latest annual diagonal, n=1 the prior year, etc. Ingesting n=0 AND
    n=1 lands the two annual snapshots reserve_deterioration_boost compares."""
    accession, primary, filing_date, _report = _nth_10k(cik, n)
    if not accession:
        raise RuntimeError(f"no 10-K at diagonal n={n}")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    instance = _get(f"{base}/{re.sub(r'.htm$', '_htm.xml', primary)}").text
    return instance, filing_date


def fetch_artifacts(ticker: str, cik: str) -> dict:
    """Pull the XBRL instance + candidate R-file HTML for a ticker's latest 10-K."""
    accession, primary, filing_date = _latest_10k(cik)
    if not accession:
        raise RuntimeError(f"{ticker}: no 10-K found")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    # The inline-XBRL instance is the primary doc with .htm → _htm.xml.
    instance_name = re.sub(r"\.htm$", "_htm.xml", primary)
    instance = _get(f"{base}/{instance_name}").text
    rfiles: list[tuple[str, str]] = []
    for fn, name in _rfile_candidates(base):
        try:
            rfiles.append((name, _get(f"{base}/{fn}").text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: R-file %s fetch failed: %s", ticker, fn, exc)
    return {"accession": accession, "filing_date": filing_date,
            "instance": instance, "rfiles": rfiles}


def extract_and_compare(ticker: str, cik: str) -> dict:
    """Run both extractors on a ticker's latest 10-K and diff them."""
    art = fetch_artifacts(ticker, cik)
    xbrl = parse_xbrl_triangles(art["instance"], insurer=ticker)
    rfile: list[dict] = []
    for _name, html in art["rfiles"]:
        rfile.extend(parse_rfile_triangles(html, insurer=ticker))
    return {
        "ticker": ticker, "filing_date": art["filing_date"],
        "n_rfiles": len(art["rfiles"]),
        "rfile_names": [n for n, _ in art["rfiles"]],
        "xbrl": xbrl, "rfile": rfile,
        "diff": diff_triangles(xbrl, rfile),
    }
