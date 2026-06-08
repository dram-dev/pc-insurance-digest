"""Concept-registry component-fact extractor — digest.parse.xbrl_facts.

Network-free: a tiny synthetic XBRL instance with a duration premium fact, an
instant claim-count fact (kept raw), and an incurred triangle fact exercises the
registry mapping, USD→millions scaling, dimension resolution, and the
loss_triangles reshape.
"""
from __future__ import annotations

from digest.parse.xbrl_facts import extract_facts, triangle_cells_from_facts

_AY24 = ('<xbrldi:explicitMember dimension="us-gaap:ShortdurationInsuranceContractsAccidentYearAxis">'
         'us-gaap:ShortDurationInsuranceContractAccidentYear2024Member</xbrldi:explicitMember>')
_AUTO = ('<xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">'
         'demo:AutoMember</xbrldi:explicitMember>')
_SEG = ('<xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
        'demo:PersonalLinesSegmentMember</xbrldi:explicitMember>')


def _instance() -> str:
    def ctx(cid, dims, period):
        return (f'<context id="{cid}"><entity><identifier scheme="s">1</identifier>'
                f'<segment>{dims}</segment></entity><period>{period}</period></context>')
    return (
        '<?xml version="1.0"?>'
        '<xbrl xmlns="http://www.xbrl.org/2003/instance"'
        ' xmlns:us-gaap="http://fasb.org/us-gaap/2025"'
        ' xmlns:srt="http://fasb.org/srt/2025"'
        ' xmlns:demo="http://demo"'
        ' xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        + ctx("prem", _SEG, "<startDate>2025-01-01</startDate><endDate>2025-12-31</endDate>")
        + ctx("cc", _AY24 + _AUTO, "<instant>2025-12-31</instant>")
        + ctx("inc", _AY24 + _AUTO, "<instant>2025-12-31</instant>")
        + '<us-gaap:PremiumsEarnedNet contextRef="prem" unitRef="u" decimals="-6">5000000000</us-gaap:PremiumsEarnedNet>'
        + '<us-gaap:ShortdurationInsuranceContractsNumberOfReportedClaims contextRef="cc" unitRef="claim" decimals="0">123456</us-gaap:ShortdurationInsuranceContractsNumberOfReportedClaims>'
        + '<us-gaap:ShortdurationInsuranceContractsIncurredClaimsAndAllocatedClaimAdjustmentExpenseNet contextRef="inc" unitRef="u" decimals="-6">800000000</us-gaap:ShortdurationInsuranceContractsIncurredClaimsAndAllocatedClaimAdjustmentExpenseNet>'
        + '</xbrl>'
    )


def test_extract_facts_registry_and_scaling():
    facts = {(f["dataset"], f["field"]): f for f in extract_facts(_instance(), insurer="DEMO")}
    assert len(facts) == 3

    prem = facts[("premiums", "premiums_earned_net")]
    assert prem["value"] == 5000.0           # 5e9 USD → $M
    assert prem["period_type"] == "duration" and prem["period_end"] == "2025-12-31"
    assert prem["segment"] == "personal_lines_segment" and prem["is_count"] == 0

    cc = facts[("claim_counts", "reported_claims")]
    assert cc["value"] == 123456.0           # counts kept raw
    assert cc["is_count"] == 1 and cc["accident_year"] == 2024 and cc["product"] == "auto"

    inc = facts[("triangle", "incurred")]
    assert inc["value"] == 800.0 and inc["accident_year"] == 2024
    assert all(f["as_of"] == "2025-12-31" for f in facts.values())


def test_triangle_reshape_from_facts():
    cells = triangle_cells_from_facts(extract_facts(_instance(), insurer="DEMO"))
    assert len(cells) == 1
    c = cells[0]
    # AY2024 valued at 2025-12-31 → dev (2025-2024+1)*12 = 24 months; lob from product
    assert c == {"insurer": "DEMO", "lob": "auto", "metric": "incurred",
                 "accident_year": 2024, "dev_period": 24,
                 "cumulative_value": 800.0, "as_of": "2025-12-31"}


def test_fact_keys_are_unique_and_stable():
    f1 = extract_facts(_instance(), insurer="DEMO")
    f2 = extract_facts(_instance(), insurer="DEMO")
    keys = [f["fact_key"] for f in f1]
    assert len(keys) == len(set(keys))                       # unique per context
    assert keys == [f["fact_key"] for f in f2]               # deterministic
