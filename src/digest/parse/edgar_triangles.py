"""ASC 944 loss-development triangles from a 10-K on EDGAR — two independent
extractors that should agree, plus a diff to cross-validate them.

Public insurers disclose incurred + paid claims-development triangles (by
accident year, per segment/LOB) in the annual 10-K under ASC 944-40-50. On
EDGAR the data is reachable two ways, and we build BOTH so each checks the
other (a triangle that both routes agree on is high-confidence):

  • parse_rfile_triangles(html)  — the XBRL-RENDERED R-file ("...Incurred and
    Paid Claims Development...(Details)", located via FilingSummary.xml). A
    human-readable pivot: columns = valuation dates (newest first), rows = a
    [segment | accident-year | lob] breadcrumb then Incurred/Paid metric rows.
    Structure-driven so it tolerates per-filer label wording.

  • parse_xbrl_triangles(instance_xml) — the standalone XBRL instance
    (<ticker>-<date>_htm.xml). Keys off the STANDARDIZED us-gaap concepts and
    the ShortdurationInsuranceContractsAccidentYearAxis dimension, so concept
    names don't vary across filers and the segment/product/SUBSEGMENT axes give
    full LOB granularity (incl. the agency/direct channel the R-file flattens).

Both emit the cell dicts db.upsert_triangle_cells() wants:
    {insurer, lob, metric ('incurred'|'paid'), accident_year, dev_period (months),
     cumulative_value (USD millions), as_of ('YYYY-12-31')}
so the downstream chain-ladder → reserving → reserve_deterioration_boost wiring
is identical regardless of which extractor wins. diff_triangles() reports where
the two agree and where they don't, so we can pick the path on evidence.

Pure / network-free — the fetch + locate + orchestration lives in
digest.edgar_triangle_extract.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter

logger = logging.getLogger(__name__)

# ── shared helpers ───────────────────────────────────────────────────────────
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_NUM_RE = re.compile(r"^\(?\$?\s*-?[\d,]+(?:\.\d+)?\)?$")
_DEV_MONTHS = 12  # ASC 944 development columns are annual; store the lag in months


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _num(s: str) -> float | None:
    s = (s or "").strip()
    if not _NUM_RE.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    v = float(re.sub(r"[(),$\s]", "", s))
    return -v if neg else v


def _dev_months(valuation_year: int, accident_year: int) -> int:
    return (valuation_year - accident_year + 1) * _DEV_MONTHS


# ── Extractor 2: XBRL instance facts (standardized concepts) ─────────────────
_C_INCURRED = "ShortdurationInsuranceContractsIncurredClaimsAndAllocatedClaimAdjustmentExpenseNet"
_C_PAID = "ShortdurationInsuranceContractsCumulativePaidClaimsAndAllocatedClaimAdjustmentExpenseNet"
_AX_ACCIDENT_YEAR = "ShortdurationInsuranceContractsAccidentYearAxis"
_AX_SEGMENT = "StatementBusinessSegmentsAxis"
_AX_PRODUCT = "ProductOrServiceAxis"
_AX_SUBSEGMENT = "SubsegmentsAxis"
# Axes that compose the LOB key, most-specific last.
_LOB_AXES = (_AX_SEGMENT, _AX_PRODUCT, _AX_SUBSEGMENT)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _member_label(member: str) -> str:
    """'pgr:PersonalLinesPropertyMember' → 'PersonalLinesProperty'."""
    base = member.rsplit(":", 1)[-1]
    return re.sub(r"Member$", "", base)


def _xbrl_lob(dims: dict[str, str]) -> str:
    """Compose a stable LOB slug from the segment/product/subsegment members,
    de-duplicating overlapping prefixes (segment 'PersonalLines' + product
    'PersonalLinesProperty' → 'personal_lines_property', + channel if present)."""
    parts: list[str] = []
    for ax in _LOB_AXES:
        m = dims.get(ax)
        if not m:
            continue
        label = re.sub(r"Segment$", "", _member_label(m))
        slug = _slug(label)
        if not slug:
            continue
        # drop a part already implied by a more specific one we'll keep
        if any(slug != p and (slug in p or p in slug) for p in parts):
            parts = [p for p in parts if not (p in slug)]
        if slug not in parts:
            parts.append(slug)
    # longest (most specific) wins as the base; append a channel suffix if any
    return "_".join(parts) if parts else "all_lines"


def parse_xbrl_triangles(instance_xml: str, *, insurer: str) -> list[dict]:
    """Triangle cells from the standalone XBRL instance. Values normalized to
    USD millions; lob carries full segment/product/subsegment granularity."""
    root = ET.fromstring(instance_xml)

    # contexts: id → (instant_year, {axis_localname: member})
    ctx: dict[str, tuple[int | None, dict[str, str]]] = {}
    for node in root:
        if _localname(node.tag) != "context":
            continue
        instant_year: int | None = None
        dims: dict[str, str] = {}
        for el in node.iter():
            ln = _localname(el.tag)
            if ln == "instant" and el.text:
                m = _YEAR_RE.search(el.text)
                instant_year = int(m.group()) if m else None
            elif ln == "explicitMember":
                ax = el.get("dimension", "").rsplit(":", 1)[-1]
                dims[ax] = (el.text or "").strip()
        ctx[node.get("id", "")] = (instant_year, dims)

    cells: list[dict] = []
    years: list[int] = []
    for el in root:
        concept = _localname(el.tag)
        metric = {_C_INCURRED: "incurred", _C_PAID: "paid"}.get(concept)
        if not metric:
            continue
        cref = el.get("contextRef")
        if cref not in ctx:
            continue
        instant_year, dims = ctx[cref]
        ay_member = dims.get(_AX_ACCIDENT_YEAR)
        if not ay_member or instant_year is None or not (el.text or "").strip():
            continue
        m = _YEAR_RE.search(ay_member)
        if not m:
            continue
        ay = int(m.group())
        if instant_year < ay:
            continue
        try:
            value = float(el.text) / 1_000_000.0  # facts are full USD → millions
        except ValueError:
            continue
        years.append(instant_year)
        cells.append({
            "insurer": insurer, "lob": _xbrl_lob(dims), "metric": metric,
            "accident_year": ay, "dev_period": _dev_months(instant_year, ay),
            "cumulative_value": round(value, 4), "instant_year": instant_year,
        })

    as_of = f"{max(years)}-12-31" if years else None
    for c in cells:
        c["as_of"] = as_of
        c.pop("instant_year", None)
    logger.info("triangles(xbrl): %s — %d cells, as_of=%s", insurer, len(cells), as_of)
    return cells


# ── Extractor 1: rendered R-file pivot (structure-driven) ────────────────────
from html.parser import HTMLParser  # noqa: E402


class _RowTable(HTMLParser):
    """Collect each <tr> as a list of cell strings; skip script/style."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("td", "th") and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if not self._skip and self._row is not None:
            self._cell.append(data)


def _scale_divisor(rows: list[list[str]]) -> float:
    """Read the '$ in Millions/Thousands' note so R-file values normalize to the
    same USD-millions scale as the XBRL route. Defaults to millions."""
    head = " ".join(c for r in rows[:3] for c in r).lower()
    if "thousand" in head:
        return 1_000.0
    if "billion" in head:
        return 0.001
    return 1.0  # millions (the usual ASC 944 presentation)


def parse_rfile_triangles(rfile_html: str, *, insurer: str) -> list[dict]:
    """Triangle cells from the XBRL-rendered claims-development R-file. Structure-
    driven: a metric row is any row whose label says incurred/paid (the stable
    discriminators) carrying multiple values under an accident-year breadcrumb."""
    rt = _RowTable()
    rt.feed(rfile_html)
    rows = [r for r in rt.rows if any(c.strip() for c in r)]
    if not rows:
        return []

    # Header: first row carrying ≥3 'Dec. 31, YYYY' valuation columns (newest-first).
    val_years: list[int] = []
    for r in rows:
        yrs = [int(_YEAR_RE.search(c).group()) for c in r
               if "31" in c and _YEAR_RE.search(c)]
        if len(yrs) >= 3:
            val_years = yrs
            break
    if not val_years:
        return []
    as_of = f"{max(val_years)}-12-31"
    divisor = _scale_divisor(rows)

    lob: str | None = None
    ay: int | None = None
    cells: list[dict] = []
    for r in rows:
        label = r[0].strip()
        low = label.lower()
        vals = r[1:]
        if not any(_num(c) is not None for c in vals):
            # breadcrumb: '[segment] | [accident-year] | [lob...]' in one cell
            if "|" in label:
                parts = [p.strip() for p in label.split("|")]
                yrs = [p for p in parts if re.fullmatch(r"(?:19|20)\d{2}", p)]
                texts = [p for p in parts
                         if p and not re.fullmatch(r"(?:19|20)\d{2}", p)]
                ay = int(yrs[0]) if yrs else None  # no year → segment aggregate
                non_seg = [t for t in texts if "segment" not in t.lower()]
                lob = _slug(" ".join(non_seg)) if non_seg else lob
            continue
        # metric row — 'incurred'/'paid' are stable across filers even when the
        # full phrase wording differs.
        metric = ("incurred" if low.startswith("incurred")
                  else "paid" if "cumulative paid" in low or low.startswith("paid")
                  else None)
        if not metric or ay is None:
            continue
        for vy, cell in zip(val_years, vals):
            v = _num(cell)
            if v is None or vy < ay:
                continue
            cells.append({
                "insurer": insurer, "lob": lob or "all_lines", "metric": metric,
                "accident_year": ay, "dev_period": _dev_months(vy, ay),
                "cumulative_value": round(v / divisor, 4), "as_of": as_of,
            })
    logger.info("triangles(rfile): %s — %d cells, as_of=%s", insurer, len(cells), as_of)
    return cells


# ── cross-validation ─────────────────────────────────────────────────────────
# The two routes derive LOB names from different vocabularies (XBRL member labels
# vs R-file breadcrumb text), so we compare on VALUES, not LOB names: a route is
# validated when every (metric, accident-year, dev, value) it emits also appears
# in the other. Triangle-level LOB correspondence is then recovered by matching
# triangles with identical cell fingerprints.
def _round(v: float) -> float:
    return round(v, 1)


def _triangles(cells: list[dict]) -> dict[tuple, dict[tuple, float]]:
    out: dict[tuple, dict[tuple, float]] = {}
    for c in cells:
        out.setdefault((c["lob"], c["metric"]), {})[
            (c["accident_year"], c["dev_period"])] = _round(c["cumulative_value"])
    return out


def _fingerprint(tri: dict[tuple, float]) -> frozenset:
    return frozenset((ay, dev, v) for (ay, dev), v in tri.items())


def diff_triangles(xbrl: list[dict], rfile: list[dict]) -> dict:
    """Cross-validate the two extractors LOB-name-agnostically.

    Headline = the (metric, AY, dev, value) multiset overlap. Also pairs each
    XBRL triangle to the R-file triangle with an identical cell fingerprint (so
    the divergent LOB namings get auto-mapped), and lists triangles only one
    route found — the signal for where a route is missing data."""
    sx = Counter((c["metric"], c["accident_year"], c["dev_period"], _round(c["cumulative_value"]))
                 for c in xbrl)
    sr = Counter((c["metric"], c["accident_year"], c["dev_period"], _round(c["cumulative_value"]))
                 for c in rfile)
    agree = sum((sx & sr).values())
    only_xbrl = sum((sx - sr).values())
    only_rfile = sum((sr - sx).values())

    tx, tr = _triangles(xbrl), _triangles(rfile)
    by_fp: dict[tuple, list[str]] = {}
    for (lob, metric), tri in tr.items():
        by_fp.setdefault((metric, _fingerprint(tri)), []).append(lob)

    pairing: list[dict] = []
    matched_rfile: set[tuple] = set()
    unmatched_xbrl: list[tuple] = []
    for (lob, metric), tri in tx.items():
        cand = by_fp.get((metric, _fingerprint(tri)))
        if cand:
            pairing.append({"xbrl_lob": lob, "rfile_lob": cand[0],
                            "metric": metric, "cells": len(tri)})
            matched_rfile.add((cand[0], metric))
        else:
            unmatched_xbrl.append((lob, metric, len(tri)))
    unmatched_rfile = [(lob, metric, len(tri)) for (lob, metric), tri in tr.items()
                       if (lob, metric) not in matched_rfile]

    return {
        "xbrl_cells": len(xbrl), "rfile_cells": len(rfile),
        "values_agree": agree, "only_xbrl_values": only_xbrl,
        "only_rfile_values": only_rfile,
        "value_agree_pct": round(100 * agree / max(len(xbrl), 1), 1),
        "xbrl_triangles": len(tx), "rfile_triangles": len(tr),
        "matched_triangles": len(pairing),
        "lob_pairing": pairing[:40],
        "unmatched_xbrl": unmatched_xbrl, "unmatched_rfile": unmatched_rfile,
    }
