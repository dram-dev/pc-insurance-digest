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


# ── CA: YTD approvals workbook (.xlsx) ────────────────────────────────────────

from datetime import datetime

import openpyxl

from digest.ingest import serff


def _make_ca_xlsx(rows: list[list]) -> bytes:
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["FILE", "NAME", "GRP #", "NAIC #", "LINE TYPE", "LINE CODE", "PROGRAM",
               "FILING TYPE", "% RATE CHNG REQ", "% RATE CHNG APPVD", "STATUS", "CLOSED DATE"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_CA_PAGE = '<a href="/x/Approval-Closed-List-YTD-4-30-26.xlsx">Approval and Closed List YTD 4/30/26</a>'


def test_ca_xlsx_filters_sorts_and_caps(monkeypatch):
    xlsx = _make_ca_xlsx([
        ["26-1", "ACME INS", "1", "111", "PERSONAL", "HOMEOWNERS", "HO PROG", "RATE", "12.5", "11.0", "APPROVED", datetime(2026, 4, 30)],
        ["26-2", "BETA CO",  "2", "222", "COMMERCIAL", "AUTO", "CA PROG", "RATE", "2.0", "2.0", "APPROVED", datetime(2026, 4, 29)],   # <5% → drop
        ["26-3", "GAMMA",    "3", "333", "PERSONAL", "FIRE", "F PROG", "RATE", "", "", "APPROVED", datetime(2026, 4, 28)],            # blank → drop
        ["26-4", "DELTA INS","4", "444", "COMMERCIAL", "OTHER LIABILITY", "X", "RATE", "-32.2", "-32.2", "CLOSED", datetime(2026, 5, 1)],
    ])

    def fake_get(u, **k):
        if u.lower().endswith((".xlsx", ".xls")):
            return type("R", (), {"content": xlsx, "text": "", "raise_for_status": lambda self: None})()
        return type("R", (), {"content": b"", "text": _CA_PAGE, "raise_for_status": lambda self: None})()

    monkeypatch.setattr(serff.requests, "get", fake_get)
    items = serff.SerffIngestor()._scrape_ca_xlsx("CA", "CA DOI", "https://x/approvals/")
    # 26-1 (12.5%) + 26-4 (-32.2%) kept; 26-2 (<5%) + 26-3 (blank) dropped; sorted by
    # Closed Date desc → 26-4 (May 1) before 26-1 (Apr 30).
    assert [it.source_id for it in items] == ["CA:26-4", "CA:26-1"]
    assert items[0].metadata["rate_change_pct"] == -32.2
    assert items[0].published_at.month == 5
    assert items[1].title == "[CA DOI] ACME INS — +12.5% on HOMEOWNERS"
    assert items[1].metadata["rate_change_approved_pct"] == 11.0


def test_ca_xlsx_no_link_returns_empty(monkeypatch):
    monkeypatch.setattr(serff.requests, "get",
                        lambda u, **k: type("R", (), {"content": b"", "text": "<p>no files</p>",
                                                      "raise_for_status": lambda self: None})())
    assert serff.SerffIngestor()._scrape_ca_xlsx("CA", "x", "https://x/") == []


def test_parse_pct_value_handles_bare_numbers_and_blanks():
    assert serff._parse_pct_value("2.7") == 2.7
    assert serff._parse_pct_value("-32.2") == -32.2
    assert serff._parse_pct_value("1,234") == 1234.0
    assert serff._parse_pct_value("-") is None
    assert serff._parse_pct_value("") is None
    assert serff._parse_pct_value("12.5%") == 12.5     # falls back to the %/bps parser
