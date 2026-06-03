"""SERFF standard-portal results parsing (network-free — no browser).

The interactive PrimeFaces flow is exercised live; here we feed `_parse_serff_
standard` canned `filingTable` pages (the positional results shape validated on
TX 2026-06-02) and assert the rate/LOB filter, cross-page dedup, and cap.
"""
from __future__ import annotations

from digest.ingest.serff import SerffIngestor, _parse_rate_change


def _page(rows: list[list[str]]) -> str:
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div id='j_idt25:filingTable' class='ui-datatable'><table><tbody>{trs}</tbody></table></div>"


# cols: company, NAIC, product, sub-type, filing type, status, tracking #
_PAGE1 = _page([
    ["Acme Insurance", "111", "Homeowners", "04.0000 HO", "Rate/Rule", "Closed-Approved", "TRK-1"],
    ["Beta Mutual",    "222", "Workers Comp", "16.0000 WC", "Rate/Rule", "Submitted", "TRK-2"],   # LOB not watched
    ["Gamma Co",       "333", "Personal Homeowners", "04.0004", "Policy Form", "Submitted", "TRK-3"],  # not rate
])
_PAGE2 = _page([
    ["Acme Insurance", "111", "Homeowners", "04.0000 HO", "Rate/Rule", "Closed-Approved", "TRK-1"],  # dup across pages
    ["Delta P&C",      "444", "Commercial Auto", "20.0000", "Rate", "Submitted", "TRK-4"],
])


def _ing() -> SerffIngestor:
    return SerffIngestor()


def test_parse_keeps_rate_and_watched_lob_only():
    items = _ing()._parse_serff_standard("TX", "Texas DOI", "https://x/", [_PAGE1, _PAGE2])
    ids = {it.source_id for it in items}
    # TRK-1 (Homeowners/Rate) + TRK-4 (Commercial Auto/Rate); TRK-2 (Workers Comp,
    # not watched) and TRK-3 (Policy Form, not a rate filing) dropped; TRK-1 deduped.
    assert ids == {"TX:TRK-1", "TX:TRK-4"}
    a = next(i for i in items if i.source_id == "TX:TRK-1")
    assert a.title == "[TX DOI] Acme Insurance — Rate/Rule on Homeowners"
    assert a.metadata["topic_hint"] == "regulatory_rate"
    assert a.metadata["state"] == "TX" and a.metadata["filing_id"] == "TRK-1"
    assert a.published_at is None          # list carries no filed date


def test_parse_respects_cap():
    ing = _ing()
    ing.max_per_state = 1
    items = ing._parse_serff_standard("TX", "Texas DOI", "https://x/", [_PAGE1, _PAGE2])
    assert len(items) == 1


def test_parse_no_table_is_empty():
    assert _ing()._parse_serff_standard("TX", "x", "https://x/", ["<div>no results</div>"]) == []


def test_rate_change_parser_handles_pct_and_bps():
    assert _parse_rate_change("requested +12.5%") == 12.5
    assert _parse_rate_change("-3%") == -3.0
    assert _parse_rate_change("1250 bps") == 12.5
    assert _parse_rate_change("no number here") is None
