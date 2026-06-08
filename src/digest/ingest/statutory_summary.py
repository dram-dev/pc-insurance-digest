"""Free high-level statutory ingestion — top-writer DPW + market share.

The big mutuals (State Farm, USAA, Liberty Mutual, Farmers, American Family) file
nothing with the SEC, so the XBRL registry can't see them. This pulls their
high-level numbers from the III "Facts + Statistics" top-writer tables (sourced
from NAIC) — direct premiums written and market share by line — so the warehouse
isn't blind to the #1 US insurer. Free, public, no credentials.

Persists to statutory_facts (source='iii'), alongside the NAIC InsData Schedule P
route (source='naic_insdata'). Triangles still come only from InsData; this is the
free high-level tier the user also wanted. parse_top_writers() is pure / testable.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests
import yaml

from digest import db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "statutory_summary.yaml"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# A top-writer row: rank | company | DPW (comma'd $000, $ only on row 1) | share.
_ROW_RE = re.compile(r"(\d{1,2})\s+([A-Za-z][A-Za-z0-9.&',\- ]{2,45}?)\s+\$?([\d,]{6,})\s+([\d.]+)")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_top_writers(html: str) -> list[tuple[int, str, int, float]]:
    """Extract the leading (rank, company, DPW_$000, market_share_pct) rows from
    an III facts page. Keeps the first contiguous rank 1..N block (the table)."""
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    table: list[tuple[int, str, int, float]] = []
    for m in _ROW_RE.finditer(txt):
        rank = int(m.group(1))
        company = m.group(2).strip()
        dpw = int(m.group(3).replace(",", ""))
        share = float(m.group(4))
        if not (1 <= rank <= 10 and dpw >= 500_000 and 0.1 <= share <= 60):
            continue
        if rank == len(table) + 1:           # extend the contiguous 1..N table
            table.append((rank, company, dpw, share))
        elif rank == 1:                       # a fresh table started — reset
            table = [(rank, company, dpw, share)]
    return table


def _facts_for_source(src: dict, html: str) -> list[dict]:
    line, year = src.get("line"), str(src.get("year", ""))
    facts: list[dict] = []
    for _rank, company, dpw, share in parse_top_writers(html):
        insurer = _slug(company)
        as_of = f"{year}-12-31" if year else None
        facts.append({
            "insurer": insurer, "source": "iii", "dataset": "premiums",
            "field": "direct_premiums_written", "line": line, "period": year,
            "value": round(dpw / 1000.0, 4), "unit": "usd_millions", "as_of": as_of,
        })
        facts.append({
            "insurer": insurer, "source": "iii", "dataset": "market_share",
            "field": "market_share", "line": line, "period": year,
            "value": share, "unit": "pct", "as_of": as_of,
        })
    return facts


def run_statutory_summary(_fetch=None) -> dict:
    """Fetch the configured III top-writer tables → statutory_facts. `_fetch(url)
    -> html` is injectable for tests; production uses requests."""
    fetch = _fetch or (lambda url: requests.get(
        url, headers={"User-Agent": _UA}, timeout=25).text)
    cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    all_facts: list[dict] = []
    n_sources = 0
    for src in cfg.get("sources", []):
        if not src.get("enabled", False):
            continue
        n_sources += 1
        try:
            html = fetch(src["url"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("statutory_summary: fetch failed for %s: %s", src["name"], exc)
            continue
        facts = _facts_for_source(src, html)
        logger.info("statutory_summary: %s → %d writers", src["name"], len(facts) // 2)
        all_facts.extend(facts)
    db.upsert_statutory_facts(all_facts)
    insurers = sorted({f["insurer"] for f in all_facts})
    return {"sources": n_sources, "facts": len(all_facts),
            "insurers": len(insurers), "insurer_list": insurers}
