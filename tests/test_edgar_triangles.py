"""EDGAR loss-triangle extractors — XBRL-instance + rendered-R-file, cross-checked.

Network-free: tiny synthetic fixtures encode the SAME personal-auto triangle two
ways (an XBRL instance and a rendered R-file) so we can assert each parser, and
that diff_triangles sees them as identical.
"""
from __future__ import annotations

from digest.parse.edgar_triangles import (
    diff_triangles,
    parse_rfile_triangles,
    parse_xbrl_triangles,
)

_INC = "ShortdurationInsuranceContractsIncurredClaimsAndAllocatedClaimAdjustmentExpenseNet"
_PAID = "ShortdurationInsuranceContractsCumulativePaidClaimsAndAllocatedClaimAdjustmentExpenseNet"

# Shared triangle (personal_auto, $M): incurred slightly favorable, paid building.
#   AY2022: incurred 100/99/98 (dev 12/24/36); paid 40/50/60
#   AY2023: incurred 105/104 (dev 12/24);      paid 42/55
#   AY2024: incurred 110 (dev 12);             paid 45
_INCURRED = {(2022, 2022): 100, (2022, 2023): 99, (2022, 2024): 98,
             (2023, 2023): 105, (2023, 2024): 104, (2024, 2024): 110}
_PAIDV = {(2022, 2022): 40, (2022, 2023): 50, (2022, 2024): 60,
          (2023, 2023): 42, (2023, 2024): 55, (2024, 2024): 45}


def _ctx(cid: str, ay: int, vyear: int) -> str:
    return (
        f'<context id="{cid}"><entity><identifier scheme="s">1</identifier><segment>'
        f'<xbrldi:explicitMember dimension="us-gaap:ShortdurationInsuranceContractsAccidentYearAxis">'
        f'us-gaap:ShortDurationInsuranceContractAccidentYear{ay}Member</xbrldi:explicitMember>'
        f'<xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">'
        f'demo:PersonalAutoMember</xbrldi:explicitMember>'
        f'</segment></entity><period><instant>{vyear}-12-31</instant></period></context>'
    )


def _build_xbrl() -> str:
    ctxs, facts = [], []
    for (ay, vy), val in _INCURRED.items():
        cid = f"i_{ay}_{vy}"
        ctxs.append(_ctx(cid, ay, vy))
        facts.append(f'<us-gaap:{_INC} contextRef="{cid}" unitRef="u" decimals="-6">{val * 1_000_000}</us-gaap:{_INC}>')
    for (ay, vy), val in _PAIDV.items():
        cid = f"p_{ay}_{vy}"
        ctxs.append(_ctx(cid, ay, vy))
        facts.append(f'<us-gaap:{_PAID} contextRef="{cid}" unitRef="u" decimals="-6">{val * 1_000_000}</us-gaap:{_PAID}>')
    return (
        '<?xml version="1.0"?>'
        '<xbrl xmlns="http://www.xbrl.org/2003/instance"'
        ' xmlns:us-gaap="http://fasb.org/us-gaap/2025"'
        ' xmlns:srt="http://fasb.org/srt/2025"'
        ' xmlns:demo="http://demo"'
        ' xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        + "".join(ctxs) + "".join(facts) + "</xbrl>"
    )


def _build_rfile() -> str:
    # columns newest-first: 2024, 2023, 2022
    val_years = [2024, 2023, 2022]
    header = ("<tr><th>Loss Development $ in Millions</th>"
              + "".join(f"<th>Dec. 31, {y} USD ($)</th>" for y in val_years) + "</tr>")
    rows = [header]
    for ay in (2022, 2023, 2024):
        rows.append(f'<tr><td>Personal Lines Segment | {ay} | Personal Auto</td>'
                    + "<td></td>" * 3 + "</tr>")
        for concept, table in (("Incurred", _INCURRED), ("Cumulative Paid", _PAIDV)):
            tds = "".join(f"<td>{table.get((ay, vy), '')}</td>" for vy in val_years)
            rows.append(f"<tr><td>{concept} Claims and Allocated Claim Adjustment Expenses, Net</td>{tds}</tr>")
    return "<html><body><table>" + "".join(rows) + "</table></body></html>"


def test_xbrl_parser_reads_triangle():
    cells = parse_xbrl_triangles(_build_xbrl(), insurer="DEMO")
    assert len(cells) == 12
    # XBRL members are CamelCase with no separators → 'personalauto' (the R-file
    # route yields 'personal_auto'; diff_triangles bridges the naming by value).
    assert all(c["lob"] == "personalauto" and c["as_of"] == "2024-12-31" for c in cells)
    inc = {(c["accident_year"], c["dev_period"]): c["cumulative_value"]
           for c in cells if c["metric"] == "incurred"}
    assert inc[(2022, 12)] == 100 and inc[(2022, 36)] == 98 and inc[(2024, 12)] == 110


def test_rfile_parser_reads_triangle():
    cells = parse_rfile_triangles(_build_rfile(), insurer="DEMO")
    assert len(cells) == 12
    paid = {(c["accident_year"], c["dev_period"]): c["cumulative_value"]
            for c in cells if c["metric"] == "paid"}
    assert paid[(2022, 12)] == 40 and paid[(2022, 36)] == 60 and paid[(2023, 24)] == 55


def test_two_extractors_cross_validate():
    xbrl = parse_xbrl_triangles(_build_xbrl(), insurer="DEMO")
    rfile = parse_rfile_triangles(_build_rfile(), insurer="DEMO")
    d = diff_triangles(xbrl, rfile)
    assert d["values_agree"] == 12
    assert d["only_xbrl_values"] == 0 and d["only_rfile_values"] == 0
    assert d["value_agree_pct"] == 100.0
    assert d["matched_triangles"] == 2  # incurred + paid


def test_diff_flags_a_disagreement():
    xbrl = parse_xbrl_triangles(_build_xbrl(), insurer="DEMO")
    rfile = parse_rfile_triangles(_build_rfile(), insurer="DEMO")
    rfile[0]["cumulative_value"] += 7  # corrupt one cell
    d = diff_triangles(xbrl, rfile)
    assert d["only_xbrl_values"] >= 1 and d["only_rfile_values"] >= 1
    assert d["value_agree_pct"] < 100.0


def test_xbrl_parser_empty_without_accident_year_facts():
    # A broker / life filer has no short-duration claims-development facts.
    empty = ('<?xml version="1.0"?><xbrl xmlns="http://www.xbrl.org/2003/instance"'
             ' xmlns:us-gaap="http://fasb.org/us-gaap/2025"></xbrl>')
    assert parse_xbrl_triangles(empty, insurer="BRK") == []
