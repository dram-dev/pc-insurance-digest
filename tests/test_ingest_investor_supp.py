"""investor_supp ingestor — triangle routing + LOB/metric detection (Lead 6).

Network-free: drives the table-routing logic directly with synthetic
parse.pdf_tables.Table objects, no live PDF fetch.
"""
from __future__ import annotations

import pytest

from digest import db
from digest.ingest.investor_supp import (
    InvestorSuppIngestor,
    _detect_lob,
    _detect_metric,
)
from digest.parse.pdf_tables import Table


@pytest.fixture
def ingestor() -> InvestorSuppIngestor:
    """Real config load (offline) — no insurers enabled, so fetch() is a no-op."""
    return InvestorSuppIngestor()


def _triangle(header_first: str) -> Table:
    return Table(
        page=2,
        header=[header_first, "12", "24", "36"],
        rows=[
            ["2023", "100", "150", "165"],
            ["2024", "110", "165", ""],
            ["2025", "120", "", ""],
        ],
    )


def test_config_loads_triangle_patterns(ingestor):
    assert ingestor.triangle_patterns, "triangle_header_patterns missing from config"
    assert any("accident year" in p for p in ingestor.triangle_patterns)


def test_route_triangles_upserts_cells(fresh_db, ingestor):
    tbl = _triangle("Accident Year — Incurred Loss Development")
    written = ingestor._route_triangles([tbl], "PGR", 2026, 2)
    assert written == 6
    rows = db.load_triangle("PGR", "all_lines", "incurred", "2026-06-30")
    assert len(rows) == 6
    assert {r["accident_year"] for r in rows} == {2023, 2024, 2025}


def test_route_triangles_skips_non_triangle(fresh_db, ingestor):
    bogus = Table(page=1, header=["Accident Year", "Premium"],
                  rows=[["Personal auto", "$1,200"]])
    # Header matches a triangle pattern but the body isn't developable → 0 cells.
    assert ingestor._route_triangles([bogus], "ALL", 2026, 1) == 0


def test_detect_metric():
    assert _detect_metric(_triangle("Cumulative Paid Loss Development")) == "paid"
    assert _detect_metric(_triangle("Incurred Loss Development")) == "incurred"
    # Ambiguous → incurred default.
    assert _detect_metric(_triangle("Accident Year Development")) == "incurred"


def test_detect_lob():
    assert _detect_lob(_triangle("Personal Auto Accident Year")) == "personal_auto"
    assert _detect_lob(_triangle("Homeowners Loss Development")) == "homeowners"
    assert _detect_lob(_triangle("Workers' Comp Triangle")) == "workers_comp"
    assert _detect_lob(_triangle("Accident Year Loss Development")) == "all_lines"


# ── Caption-aware detection (real PDFs carry LOB/basis in the caption, not the
#    column-header row, which is just dev periods) ──────────────────────────


def _captioned_triangle(caption: str) -> Table:
    """A triangle whose header row is bare dev periods + a blank label column —
    the realistic shape — with LOB/basis only in the caption above the grid."""
    return Table(
        page=2,
        caption=caption,
        header=["", "12", "24", "36"],
        rows=[
            ["2023", "100", "150", "165"],
            ["2024", "110", "165", ""],
            ["2025", "120", "", ""],
        ],
    )


def test_detect_metric_from_caption():
    assert _detect_metric(_captioned_triangle("Personal Auto — Cumulative Paid Loss Development")) == "paid"
    assert _detect_metric(_captioned_triangle("Homeowners Incurred Loss Development")) == "incurred"


def test_detect_lob_from_caption():
    assert _detect_lob(_captioned_triangle("Personal Auto — Incurred Loss Development")) == "personal_auto"
    assert _detect_lob(_captioned_triangle("Homeowners Cumulative Paid Loss Development")) == "homeowners"
    # No LOB anywhere → fallback.
    assert _detect_lob(_captioned_triangle("Accident Year Loss Development")) == "all_lines"


def test_header_matches_uses_caption(ingestor):
    # Triangle keyword lives only in the caption; the header is bare dev periods.
    assert _captioned_triangle("Accident Year Loss Development").header_matches(
        ingestor.triangle_patterns
    )
    # Neither caption nor header carries a triangle pattern → no match.
    plain = Table(page=1, header=["Region", "Premium"], rows=[["West", "10"]])
    assert not plain.header_matches(ingestor.triangle_patterns)


def test_route_triangles_collision_guard(fresh_db, ingestor):
    # Two unlabeled triangles both fall back to (all_lines, incurred); the guard
    # must keep them distinct so the second doesn't overwrite the first.
    t1 = _captioned_triangle("Accident Year Loss Development")
    t2 = _captioned_triangle("Accident Year Loss Development")
    written = ingestor._route_triangles([t1, t2], "PGR", 2026, 2)
    assert written == 12
    assert len(db.load_triangle("PGR", "all_lines", "incurred", "2026-06-30")) == 6
    assert len(db.load_triangle("PGR", "all_lines_2", "incurred", "2026-06-30")) == 6
