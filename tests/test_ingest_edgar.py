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
