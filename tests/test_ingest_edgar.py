"""Tests for the lifted EDGAR content helpers in digest_core.ingest.edgar.

TextExtractor + find_exhibit_url are pure HTML parsing; fetch_html_text /
fetch_8k_content are exercised with mocked requests (delay_sec=0 keeps them
instant).
"""
from __future__ import annotations

from digest_core.ingest import edgar as core_edgar


def test_text_extractor_strips_tags_and_skips_script():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>Hello   world</p><script>var a=1;</script>"
        "<p>Second\npart</p></body></html>"
    )
    p = core_edgar.TextExtractor()
    p.feed(html)
    text = p.get_text()
    assert "Hello world" in text
    assert "Second part" in text
    assert "var a=1" not in text   # script content skipped
    assert "color:red" not in text  # style content skipped


def test_text_extractor_respects_max_chars():
    p = core_edgar.TextExtractor()
    p.feed("<p>" + "x" * 100 + "</p>")
    assert len(p.get_text(max_chars=10)) == 10


def test_find_exhibit_url_picks_ex99_row_and_absolutizes():
    index_html = """
    <table>
      <tr><td>1</td><td>10-K</td><td><a href="/Archives/x/primary.htm">primary.htm</a></td></tr>
      <tr><td>2</td><td>EX-99.1</td><td><a href="/Archives/x/ex991.htm">ex991.htm</a></td></tr>
    </table>
    """
    url = core_edgar.find_exhibit_url(index_html, "https://www.sec.gov/idx")
    assert url == "https://www.sec.gov/Archives/x/ex991.htm"


def test_find_exhibit_url_none_when_absent():
    index_html = "<tr><td>10-K</td><td><a href='/x/primary.htm'>primary</a></td></tr>"
    assert core_edgar.find_exhibit_url(index_html, "base") is None


def test_fetch_html_text_strips_and_handles_failure(monkeypatch):
    class _Resp:
        text = "<p>Press <b>release</b> body</p>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(core_edgar.requests, "get", lambda *a, **k: _Resp())
    assert core_edgar.fetch_html_text("https://x", {}, delay_sec=0) == "Press release body"

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(core_edgar.requests, "get", boom)
    assert core_edgar.fetch_html_text("https://x", {}, delay_sec=0) is None


def test_fetch_8k_content_follows_exhibit(monkeypatch):
    index_html = '<tr><td>EX-99.1</td><td><a href="/Archives/x/ex991.htm">ex</a></td></tr>'
    exhibit_html = "<p>Q1 results: combined ratio 92%</p>"

    def fake_get(url, headers=None, timeout=None):
        class _R:
            def raise_for_status(self):
                pass
        r = _R()
        r.text = exhibit_html if url.endswith("ex991.htm") else index_html
        return r

    monkeypatch.setattr(core_edgar.requests, "get", fake_get)
    out = core_edgar.fetch_8k_content("320193", "0000320193-26-000001", {}, delay_sec=0)
    assert out == "Q1 results: combined ratio 92%"


# ── Lead 5: reserve-discussion excerpt from PC's edgar shell ──────────────────
from digest.ingest.edgar import _reserve_excerpt  # noqa: E402


def test_reserve_excerpt_pulls_reserve_language():
    text = (
        "Item 1. Business. " + ("filler " * 200)
        + "The Company recorded unfavorable prior-year reserve development of "
        + "$120 million, reflecting reserve strengthening in the auto line. "
        + ("more " * 100)
    )
    out = _reserve_excerpt(text)
    assert "prior-year reserve development" in out
    assert "reserve strengthening" in out
    assert len(out) <= 3000


def test_reserve_excerpt_empty_when_no_reserve_language():
    assert _reserve_excerpt("Net premiums written rose 5% on strong retention.") == ""
    assert _reserve_excerpt("") == ""


def test_reserve_excerpt_capped_across_many_matches():
    text = ("IBNR reserves for unpaid claims. " * 500)
    out = _reserve_excerpt(text)
    assert 0 < len(out) <= 3000


# ── Wave 4 fix: financial excerpt + per-form coverage + content-age policy ─────
from datetime import datetime, timezone  # noqa: E402

from digest.ingest.edgar import (  # noqa: E402
    _financial_excerpt,
    _select_filings,
    _content_age_ok,
    _MAX_PER_FORM,
)


def test_financial_excerpt_pulls_results_with_digits():
    text = (
        "Item 1. Business. " + ("filler " * 300)
        + "Our combined ratio was 96.0 for 2025, net premiums written rose to "
        + "$62.1 billion, and net income was $8.5 billion. " + ("tail " * 100)
    )
    out = _financial_excerpt(text)
    assert "combined ratio" in out and "96.0" in out
    assert "net premiums written" in out


def test_financial_excerpt_skips_label_only_and_empty():
    # markers present but no nearby digit → nothing captured
    assert _financial_excerpt(
        "We discuss our combined ratio philosophy and underwriting margin goals."
    ) == ""
    assert _financial_excerpt("") == ""


def _recent(rows):
    """Build an EDGAR filings.recent-shaped dict from (form,date,acc,doc) rows."""
    return {
        "form": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[2] for r in rows],
        "primaryDocument": [r[3] for r in rows],
    }


def test_select_filings_keeps_latest_10k_even_when_buried():
    # 50 Form 4s (irrelevant) bury the 10-K well past any flat top-N window.
    rows = [("4", f"2026-05-{d % 28 + 1:02d}", f"acc4-{d}", "d.htm") for d in range(50)]
    rows += [
        ("8-K", "2026-04-15", "acc8k", "p8.htm"),
        ("10-K", "2026-03-02", "acc10k", "p10k.htm"),
        ("10-Q", "2026-05-04", "acc10q", "p10q.htm"),
    ]
    sel = _select_filings(_recent(rows), is_fund=False)
    forms = {s["form"] for s in sel}
    assert "10-K" in forms and "10-Q" in forms and "8-K" in forms
    assert "4" not in forms  # Form 4 is not a relevant form
    assert any(s["accession"] == "acc10k" for s in sel)


def test_select_filings_caps_per_form_and_takes_most_recent():
    rows = [("8-K", f"2026-{m:02d}-15", f"acc-{m}", "p.htm") for m in range(1, 13)]
    eightk = [s for s in _select_filings(_recent(rows), is_fund=False) if s["form"] == "8-K"]
    assert len(eightk) == _MAX_PER_FORM["8-K"]            # capped at 8 of 12
    assert all(s["filing_date"] >= "2026-05-15" for s in eightk)  # the most-recent 8


def test_select_filings_13f_only_for_funds():
    rows = [("13F-HR", "2026-02-14", "acc13f", "x.htm")]
    assert _select_filings(_recent(rows), is_fund=False) == []
    fund = _select_filings(_recent(rows), is_fund=True)
    assert len(fund) == 1 and fund[0]["form"] == "13F-HR"


def test_content_age_ok_per_form_caps():
    now = datetime(2026, 6, 7, tzinfo=timezone.utc)
    d = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    assert _content_age_ok("10-K", d("2026-03-02"), now) is True    # ~97d ≤ 400
    assert _content_age_ok("10-K", d("2025-01-01"), now) is False   # >400d
    assert _content_age_ok("8-K", d("2026-05-20"), now) is True     # ~18d ≤ 31
    assert _content_age_ok("8-K", d("2026-03-02"), now) is False    # ~97d > 31
    assert _content_age_ok("10-K", None, now) is False
    assert _content_age_ok("13F-HR", d("2026-06-01"), now) is False  # no content for 13F


def test_fetch_8k_content_respects_max_chars(monkeypatch):
    index_html = '<tr><td>EX-99.1</td><td><a href="/Archives/x/ex991.htm">ex</a></td></tr>'
    exhibit_html = "<p>" + ("Z" * 5000) + "</p>"

    def fake_get(url, headers=None, timeout=None):
        class _R:
            def raise_for_status(self):
                pass
        r = _R()
        r.text = exhibit_html if url.endswith("ex991.htm") else index_html
        return r

    monkeypatch.setattr(core_edgar.requests, "get", fake_get)
    out = core_edgar.fetch_8k_content(
        "320193", "0000320193-26-000001", {}, delay_sec=0, max_chars=100
    )
    assert out is not None and len(out) == 100
