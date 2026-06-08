"""Canonical LOB mapping — unify source-specific LOB strings onto one taxonomy."""
from __future__ import annotations

import pytest

from digest import db
from digest.parse.lob_canonical import CANONICAL_LOBS, canonicalize_lob


@pytest.mark.parametrize("raw,expected", [
    # SEC-XBRL member slugs (the messy ones)
    ("berkshire_hathaway_insurance_group_auto_liability_insurance_geico", "personal_auto"),
    ("auto_insurance_physical_damage_coverage", "personal_auto"),
    ("personal_lines_vehicles_direct_liability", "personal_auto"),
    ("home_owners", "homeowners"),
    ("personal_lines_insurance_homeowner", "homeowners"),
    ("north_america_workers_compensation", "workers_comp"),
    ("us_workers_compensation", "workers_comp"),
    ("global_reinsurance_casualty", "reinsurance"),
    ("assumed_reinsurance", "reinsurance"),
    ("north_america_general_liability", "general_liability"),
    ("overseas_general_casualty", "general_liability"),     # underscore-joined 'casualty'
    ("usexcess_casualty", "general_liability"),
    ("monoline_excess_product_line", "general_liability"),
    ("north_america_non_casualty", "commercial_property"),  # 'non-casualty' ≠ casualty
    ("usfinancial_lines", "professional_liability"),
    ("medical_professional_liability_insurance", "medical_malpractice"),
    ("marine", "specialty"),
    ("excessand_surplus_lines_insurance", "specialty"),
    ("package_business", "commercial_multi_peril"),
    ("pcbusiness_insurance_automobiles", "commercial_auto"),  # business+auto → commercial
    ("uspersonal_insurance", "personal_lines"),
    # statutory line names
    ("personal_auto", "personal_auto"),
    ("workers_comp", "workers_comp"),
    # exact override + fallbacks
    ("markel_insurance_excluding_global_reinsurance_division", "commercial_lines"),
    ("group_policies", "other"),
    ("", "other"),
    (None, "other"),
])
def test_canonicalize_lob(raw, expected):
    assert canonicalize_lob(raw) == expected


def test_every_output_is_in_the_taxonomy():
    for s in ["auto", "homeowners", "workers comp", "totally_unknown_line_xyz", ""]:
        assert canonicalize_lob(s) in CANONICAL_LOBS


def test_upsert_triangle_cells_populates_canonical_lob(fresh_db):
    db.upsert_triangle_cells([{
        "insurer": "X", "lob": "global_reinsurance_casualty", "metric": "incurred",
        "accident_year": 2024, "dev_period": 12, "cumulative_value": 100.0,
        "as_of": "2024-12-31",
    }])
    with db.get_conn() as c:
        assert c.execute("SELECT canonical_lob FROM loss_triangles").fetchone()[0] == "reinsurance"


def test_upsert_statutory_facts_populates_canonical_lob(fresh_db):
    db.upsert_statutory_facts([{
        "insurer": "state_farm", "source": "iii", "dataset": "premiums",
        "field": "direct_premiums_written", "line": "homeowners", "value": 1.0,
    }])
    with db.get_conn() as c:
        assert c.execute("SELECT canonical_lob FROM statutory_facts").fetchone()[0] == "homeowners"
