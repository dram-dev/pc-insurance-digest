"""NAIC Schedule P parser — long-form export → triangles + premium facts.

Network-free: a synthetic homeowners Schedule P (one company, 3 accident years,
$000 values) exercises the column mapping, $000→$M scaling, triangle reshape, and
the once-per-accident-year premium fact.
"""
from __future__ import annotations

from digest.parse.schedule_p import parse_schedule_p

_CMAP = {
    "company": "Company Name", "line": "Line of Business",
    "accident_year": "Accident Year", "valuation_year": "Valuation Year",
    "incurred": "Incurred Losses and DCC", "paid": "Cumulative Paid Losses and DCC",
    "earned_premium": "Premiums Earned",
}


def _row(ay, vy, inc, paid, prem):
    return {"Company Name": "State Farm", "Line of Business": "Homeowners/Farmowners",
            "Accident Year": str(ay), "Valuation Year": str(vy),
            "Incurred Losses and DCC": inc, "Cumulative Paid Losses and DCC": paid,
            "Premiums Earned": prem}


_RECORDS = [
    _row(2022, 2022, "1,000,000", "400,000", "2,000,000"),
    _row(2022, 2023, "1,050,000", "700,000", "2,000,000"),
    _row(2022, 2024, "1,030,000", "900,000", "2,000,000"),
    _row(2023, 2023, "1,100,000", "450,000", "2,100,000"),
    _row(2023, 2024, "1,120,000", "760,000", "2,100,000"),
    _row(2024, 2024, "1,200,000", "500,000", "2,300,000"),
]


def test_parse_schedule_p_triangles_and_scaling():
    cells, facts = parse_schedule_p(_RECORDS, _CMAP, line_map={"Homeowners/Farmowners": "homeowners"},
                                    value_scale=0.001)  # $000 → $M
    assert len(cells) == 12                                   # (3+2+1) AY × incurred+paid
    assert all(c["insurer"] == "state_farm" and c["lob"] == "homeowners"
               and c["as_of"] == "2024-12-31" for c in cells)
    inc = {(c["accident_year"], c["dev_period"]): c["cumulative_value"]
           for c in cells if c["metric"] == "incurred"}
    paid = {(c["accident_year"], c["dev_period"]): c["cumulative_value"]
            for c in cells if c["metric"] == "paid"}
    assert inc[(2022, 12)] == 1000.0 and inc[(2022, 36)] == 1030.0  # $000 → $M
    assert inc[(2024, 12)] == 1200.0
    assert paid[(2022, 36)] == 900.0 and paid[(2023, 24)] == 760.0


def test_premium_facts_once_per_accident_year():
    _, facts = parse_schedule_p(_RECORDS, _CMAP, value_scale=0.001)
    by_ay = {f["accident_year"]: f["value"] for f in facts}
    assert by_ay == {2022: 2000.0, 2023: 2100.0, 2024: 2300.0}    # latest-diagonal only
    assert all(f["source"] == "naic_insdata" and f["dataset"] == "premiums" for f in facts)


def test_empty_records_yield_nothing():
    assert parse_schedule_p([], _CMAP) == ([], [])
