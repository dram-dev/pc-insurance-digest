"""NAIC Schedule P → loss triangles + statutory summary facts.

Schedule P of the statutory annual statement is the canonical P&C loss-development
disclosure for EVERY US insurer — including the big mutuals (State Farm, USAA,
Liberty Mutual, Farmers, Nationwide) that file nothing with the SEC, so this is
the only route to their triangles. NAIC InsData exports the annual-statement data;
this parser takes the exported rows (long form: one row per company × line ×
accident-year × valuation-year) and produces:

  • loss_triangles cells (incurred + paid) — the SAME shape the XBRL extractor
    emits, so the chain-ladder reserving chain feeds identically; and
  • statutory summary facts (earned premium by line / accident year).

Format-driven: a `column_map` (config/naic_insdata.yaml) maps the export's
columns to canonical fields and statutory line names to canonical LOBs, so it
adapts to whatever an InsData query produced without code changes. Pure /
network-free — the file-drop loader lives in digest.ingest.naic_insdata.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DEV_MONTHS = 12
_NUM_RE = re.compile(r"^\(?-?\$?\s*[\d,]+(?:\.\d+)?\)?$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def _num(value) -> float | None:
    """Parse an accounting number: '1,234', '(50)', '$ 1,000', '' → float|None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or not _NUM_RE.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    v = float(re.sub(r"[(),$\s]", "", s))
    return -v if neg else v


def _int(value) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_schedule_p(
    records: list[dict],
    column_map: dict[str, str],
    *,
    line_map: dict[str, str] | None = None,
    value_scale: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    """Structure Schedule P export rows into (triangle_cells, summary_facts).

    `records`     — list of dict rows (e.g. csv.DictReader output).
    `column_map`  — canonical field → source column name. Recognized canonical
                    keys: company, line, accident_year, valuation_year, incurred,
                    paid, earned_premium.
    `line_map`    — statutory line string → canonical LOB slug (else slugified).
    `value_scale` — multiply raw values (Schedule P is usually $000 → pass 0.001
                    to store USD millions, matching the XBRL facts).
    """
    cm = column_map
    line_map = line_map or {}
    parsed: list[tuple] = []
    val_years: list[int] = []
    for r in records:
        company = (r.get(cm.get("company", "")) or "").strip()
        line_raw = (r.get(cm.get("line", "")) or "").strip()
        ay = _int(r.get(cm.get("accident_year", "")))
        vy = _int(r.get(cm.get("valuation_year", "")))
        if not company or not line_raw or ay is None or vy is None:
            continue
        parsed.append((_slug(company), line_map.get(line_raw, _slug(line_raw)), ay, vy, r))
        val_years.append(vy)

    if not parsed:
        return [], []
    as_of = f"{max(val_years)}-12-31"
    latest_vy = max(val_years)

    cells: list[dict] = []
    facts: list[dict] = []
    seen_premium: set[tuple] = set()
    for insurer, lob, ay, vy, r in parsed:
        if vy < ay:
            continue
        dev = (vy - ay + 1) * _DEV_MONTHS
        for metric in ("incurred", "paid"):
            col = cm.get(metric)
            v = _num(r.get(col)) if col else None
            if v is None:
                continue
            cells.append({
                "insurer": insurer, "lob": lob, "metric": metric,
                "accident_year": ay, "dev_period": dev,
                "cumulative_value": round(v * value_scale, 4), "as_of": as_of,
            })
        # Earned premium is an accident-year level (not a development series): take
        # it from the latest valuation diagonal, once per (insurer, lob, AY).
        prem_col = cm.get("earned_premium")
        pv = _num(r.get(prem_col)) if prem_col else None
        key = (insurer, lob, ay)
        if pv is not None and vy == latest_vy and key not in seen_premium:
            seen_premium.add(key)
            facts.append({
                "insurer": insurer, "source": "naic_insdata", "dataset": "premiums",
                "field": "premiums_earned_statutory", "line": lob, "accident_year": ay,
                "period": str(latest_vy), "value": round(pv * value_scale, 4),
                "unit": "usd_millions", "as_of": as_of,
            })

    logger.info("schedule_p: %d triangle cells + %d premium facts across %d insurers",
                len(cells), len(facts), len({c["insurer"] for c in cells}))
    return cells, facts
