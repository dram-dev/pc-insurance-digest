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
