"""ASC 944 text-layer development-triangle parser (Lead 6).

Network-free: feeds `parse_development_text` synthetic 10-K page text shaped like
the real US-GAAP loss-development disclosure (borderless, per-segment, incurred +
paid, with trailing IBNR / claim-count columns). Mirrors the layout pdfplumber's
grid detector can't reconstruct but the text layer carries cleanly.
"""
from __future__ import annotations

from digest.parse.triangles import parse_development_text

# One business segment, both metrics. Each accident-year row is left-aligned:
# the cumulative development values come first, then the trailing IBNR and claim-
# count columns (incurred only) that the parser must drop.
_AGENCY_LIABILITY = """\
Personal Lines - Vehicles - Agency - Liability
($ in millions) As of
Incurred Claims and Allocated Claim Adjustment Expenses, Net of Reinsurance December 31, 2025
For the years ended December 31, Expected Cumulative Number
Accident Year 20211 20221 20231 20241 2025 Reported Claims Counts
2021 $ 6,716 $ 6,862 $ 6,936 $ 6,943 $ 6,831 $ 0 885,914
2022 7,077 7,302 7,226 7,222 135 842,281
2023 8,616 8,365 8,260 183 902,996
2024 9,700 9,345 541 936,963
2025 11,277 2,097 1,023,941
Total $ 42,935
Cumulative Paid Claims and Allocated Claim Adjustment Expenses, Net of Reinsurance
Accident Year 20211 20221 20231 20241 2025
2021 $ 2,855 $ 5,239 $ 6,183 $ 6,569 $ 6,727
2022 3,019 5,564 6,486 6,870
2023 3,527 6,311 7,408
2024 3,753 6,935
2025 4,427
Total $ 32,367
All outstanding liabilities before 2021, net of reinsurance1 94
Liabilities for claims and claim adjustment expenses, net of reinsurance $ 10,662
1 Required supplementary information (unaudited)
"""

# A second segment with a (-) adverse IBNR token and different magnitudes, to
# confirm multiple triangles on one page stay distinct and parens don't leak.
_PHYSICAL_DAMAGE = """\
Personal Lines - Vehicles - Agency - Physical Damage
Incurred Claims and Allocated Claim Adjustment Expenses, Net of Reinsurance December 31, 2025
Accident Year 20211 20221 20231 20241 2025 Reported Claims Counts
2021 $ 4,708 $ 4,624 $ 4,629 $ 4,619 $ 4,619 $ 0 2,106,210
2022 5,429 5,545 5,584 5,546 (8) 2,033,905
2023 5,775 5,880 5,909 21 2,118,287
2024 6,214 6,272 (18) 2,134,899
2025 6,508 (203) 2,246,453
Total $ 28,854
"""


def _cells(text: str, insurer: str = "PGR"):
    return parse_development_text([text], insurer=insurer)


def test_parses_both_metrics_into_a_5x5_triangle():
    cells = _cells(_AGENCY_LIABILITY)
    incurred = [c for c in cells if c["metric"] == "incurred"]
    paid = [c for c in cells if c["metric"] == "paid"]
    # 5+4+3+2+1 developable cells per metric.
    assert len(incurred) == 15
    assert len(paid) == 15


def test_lob_is_slugged_from_segment_caption():
    cells = _cells(_AGENCY_LIABILITY)
    assert {c["lob"] for c in cells} == {"personal_lines_vehicles_agency_liability"}


def test_as_of_read_from_incurred_header():
    assert {c["as_of"] for c in _cells(_AGENCY_LIABILITY)} == {"2025-12-31"}


def test_dev_periods_are_annual_in_months():
    cells = _cells(_AGENCY_LIABILITY)
    ay2021 = sorted((c for c in cells
                     if c["metric"] == "incurred" and c["accident_year"] == 2021),
                    key=lambda c: c["dev_period"])
    assert [c["dev_period"] for c in ay2021] == [12, 24, 36, 48, 60]
    assert ay2021[0]["cumulative_value"] == 6716.0
    assert ay2021[-1]["cumulative_value"] == 6831.0


def test_trailing_ibnr_and_claim_counts_are_dropped():
    cells = _cells(_AGENCY_LIABILITY)
    # AY2022 incurred is left-aligned to 4 dev columns; the 4th value is the
    # last cumulative (7,222), NOT the IBNR (135) or the claim count (842,281).
    ay2022 = [c for c in cells
              if c["metric"] == "incurred" and c["accident_year"] == 2022]
    assert len(ay2022) == 4
    assert max(c["dev_period"] for c in ay2022) == 48
    assert {c["cumulative_value"] for c in ay2022} == {7077.0, 7302.0, 7226.0, 7222.0}
    # The claim count must never appear as a triangle value.
    assert all(c["cumulative_value"] != 842281.0 for c in cells)


def test_subtotal_and_footnote_rows_skipped():
    # 'Total $ ...', 'All outstanding ...', 'Liabilities for ...' are not AY rows.
    cells = _cells(_AGENCY_LIABILITY)
    assert all(1980 <= c["accident_year"] <= 2100 for c in cells)
    assert all(c["cumulative_value"] not in (42935.0, 32367.0, 10662.0) for c in cells)


def test_multiple_segments_stay_distinct():
    cells = parse_development_text([_AGENCY_LIABILITY, _PHYSICAL_DAMAGE], insurer="PGR")
    lobs = {c["lob"] for c in cells}
    assert lobs == {
        "personal_lines_vehicles_agency_liability",
        "personal_lines_vehicles_agency_physical_damage",
    }


def test_adverse_paren_ibnr_does_not_leak_into_values():
    # Physical Damage AY2025 row: '6,508 (203) 2,246,453' → only 6,508 is a dev cell.
    cells = _cells(_PHYSICAL_DAMAGE)
    ay2025 = [c for c in cells if c["accident_year"] == 2025]
    assert len(ay2025) == 1
    assert ay2025[0]["cumulative_value"] == 6508.0
    assert all(c["cumulative_value"] != -203.0 for c in cells)


def test_no_asc944_tables_returns_empty():
    plain = "Net premiums written\n(millions) 2025 2024\nPersonal Lines 41,000 38,000\n"
    assert parse_development_text([plain], insurer="PGR") == []
