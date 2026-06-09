"""Reported combined-ratio extraction from investor-supplement tables — picks the
consolidated GAAP figure by VALIDATING against an anchor, never by guessing."""
from __future__ import annotations

from digest.parse.combined_ratio import combined_ratio_candidates, parse_combined_ratio
from digest.parse.pdf_tables import Table


def _t(rows):
    return Table(page=1, header=[], rows=rows)


def test_validated_pick_selects_consolidated_total():
    tables = [_t([
        ["Combined ratio2", "85.4", "86.1"],            # a segment
        ["Combined ratio", "75.1", "98.3"],             # a volatile segment
        ["Combined ratio", "87.4", "88.8"],             # consolidated total
        ["Statutory combined ratio", "87.1", "88.2"],   # statutory — skipped (GAAP only)
    ])]
    res = parse_combined_ratio(tables, insurer="PGR", anchor=0.874)
    assert res["combined_ratio"] == 0.874               # the candidate matching the anchor
    assert 0.871 not in res["candidates"]               # statutory excluded
    assert 0.854 in res["candidates"]


def test_none_when_nothing_reconciles_with_anchor():
    tables = [_t([["Combined ratio", "75.1", "98.3"]])]  # nearest is 0.751
    assert parse_combined_ratio(tables, insurer="X", anchor=0.90) is None  # >3 pts off


def test_none_without_anchor():
    tables = [_t([["Combined ratio", "87.4"]])]
    assert parse_combined_ratio(tables, insurer="X", anchor=None) is None


def test_candidates_skip_statutory_and_parse_percent_forms():
    tables = [_t([
        ["Combined ratio", "87.4%"],
        ["Statutory combined ratio", "87.1"],
        ["Combined ratio", "(101.2)"],                  # parenthesised → still a value
    ])]
    assert combined_ratio_candidates(tables) == [0.874, 1.012]
